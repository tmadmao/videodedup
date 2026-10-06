# -*- coding: utf-8 -*-
"""
本地视频查重小工具  Video Dedup Tool  v1.2.2
=====================================================================
功能一览
  1. 选择本地文件夹，递归扫描子目录内所有视频文件（mp4/mkv/mov/avi/flv ...）
  2. 扫描完成后完整列出全部视频（即使没有重复，列表也完整显示，不空白）
  3. 每行展示：缩略预览图 / 文件路径 / 文件大小 / 分辨率 / 时长 / 码率 / 编码格式
     缩略图来源可切换：封面（首帧，自动避开黑场）/ 内容帧(1/4 处) / 中间帧(1/2 处)，
     其中"封面帧"最适合肉眼比对是否同一来源
  4. 所有列均可点击排序（时长 / 大小 / 分辨率 / 码率 / 文件名 / 路径…），再次点击切换升降序；
     并提供"平铺全部"视图，可按视频长度排序，快速定位时长几乎一致的可疑项
  5. 双通道自动识别同源视频：
       ① pHash 通道   —— 对整体调色 / 亮度偏移 / 分辨率变化鲁棒
       ② 灰度掩码通道 —— 忽略黑/白像素，抗水印 / 黑边 / 烧制字幕
     两个通道任一命中即判重。复制改名、二次转码、加水印、轻微裁切都能认出来
  6. 自动分组（带「组代表互验」，避免把同系列的不同视频并成一大组）；
     每组给出"建议保留"（按 时长→分辨率→画质→码率→帧率→大小 逐级淘汰，并说明理由）
  7. 人工勾选后执行：打开所在文件夹 / 播放预览 / 移入回收站 / 移动到备份文件夹
  8. 简体中文 GUI（tkinter）

隐私与安全
  * 全部运算在本地完成，代码中没有任何网络请求（无 requests / urllib / socket / http 调用）
  * 不上传视频或图片、不调用任何云端模型
  * 绝不自动删除：所有删除类操作都必须先人工勾选，并二次确认（默认走系统回收站）

依赖（见 requirements.txt）：
  opencv-python  pillow  numpy  imagehash(可选)  pymediainfo(可选)  send2trash(可选)
  不需要 ffmpeg：元信息来自 pymediainfo 自带的 MediaInfo，解码来自 OpenCV 内置的 FFmpeg

命令行模式（可选，用于批量/无人值守）：
  python video_dedup.py --scan "D:\\videos" --out report.json --csv report.csv
  python video_dedup.py --selftest
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

# ----------------------------------------------------------------------------
# 可选依赖：任何一个缺失都只降级，不影响主流程运行
# ----------------------------------------------------------------------------
_IMPORT_ERRORS = {}
try:
    import numpy as np
except Exception as e:  # pragma: no cover
    np = None
    _IMPORT_ERRORS["numpy"] = str(e)

try:
    import cv2
except Exception as e:  # pragma: no cover
    cv2 = None
    _IMPORT_ERRORS["opencv-python"] = str(e)

try:
    import tkinter as tk
    import tkinter.ttk as ttk
    import tkinter.filedialog as tkfile
    import tkinter.messagebox as tkmsg

    HAS_TK = True
except Exception as e:  # pragma: no cover
    tk = None
    ttk = None
    tkfile = None
    tkmsg = None
    HAS_TK = False
    _IMPORT_ERRORS["tkinter"] = str(e)

try:
    from PIL import Image, ImageTk

    # 兼容 Pillow>=10：ANTIALIAS / LANCZOS 已迁移到 Image.Resampling
    if not hasattr(Image, "Resampling"):  # 极老版本
        pass
    else:
        if not hasattr(Image, "LANCZOS"):
            Image.LANCZOS = Image.Resampling.LANCZOS  # type: ignore[attr-defined]
        if not hasattr(Image, "ANTIALIAS"):
            Image.ANTIALIAS = Image.Resampling.LANCZOS  # type: ignore[attr-defined]
except Exception as e:  # pragma: no cover
    Image = None
    ImageTk = None
    _IMPORT_ERRORS["pillow"] = str(e)

try:
    import imagehash

    if Image is None:
        raise ImportError("Pillow 不可用，imagehash 无法使用")
except Exception as e:
    imagehash = None
    _IMPORT_ERRORS["imagehash"] = str(e)

try:
    from pymediainfo import MediaInfo
except Exception as e:
    MediaInfo = None
    _IMPORT_ERRORS["pymediainfo"] = str(e)

try:
    from send2trash import send2trash
except Exception as e:
    send2trash = None
    _IMPORT_ERRORS["send2trash"] = str(e)

HAS_CV2 = cv2 is not None
HAS_NUMPY = np is not None
HAS_PIL = Image is not None and ImageTk is not None
HAS_IMAGEHASH = imagehash is not None
HAS_MI = MediaInfo is not None
HAS_TRASH = send2trash is not None

# 静默 OpenCV/FFmpeg 在遇到损坏文件时的刷屏警告（不影响功能）
if HAS_CV2:
    try:
        cv2.setLogLevel(0)  # LOG_LEVEL_SILENT
    except Exception:
        try:
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
        except Exception:
            pass


# ============================================================================
# 一、常量与基础工具
# ============================================================================

APP_NAME = "本地视频查重工具"
APP_VER = "1.2.2"

# 识别的视频扩展名
VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".mts", ".webm", ".3gp", ".3g2", ".rmvb", ".rm", ".vob",
    ".f4v", ".asf", ".divx", ".ogv", ".dat", ".mp2", ".mpe", ".mxf", ".m3u8",
}

# 缩略图缓存目录（系统临时目录，不联网）
THUMB_DIR = Path(os.environ.get("TEMP") or os.path.expanduser("~")) / "video_dedup_thumbs"
CACHE_FILE = Path(os.path.expanduser("~")) / ".video_dedup_cache.json"
CONFIG_FILE = Path(os.path.expanduser("~")) / ".video_dedup_config.json"

SAMPLE_FRAMES_DEFAULT = 12      # 每个视频均匀采样的帧数（用于 pHash）
DEFAULT_THRESHOLD = 0.80        # pHash 通道默认相似度阈值（≈ 平均汉明距离 ≤ 12.8 / 64）
ROW_THUMB_SIZE = (124, 70)      # 列表行缩略图尺寸
BIG_THUMB_SIZE = (384, 216)     # 右侧预览大图尺寸
RIGHT_PANEL_W = 400             # 右侧预览面板固定宽度

# ---- 32×32 灰度指纹 + 掩码比对（用来抗水印 / 黑边 / 烧制字幕）----
# 思路：把每帧强行压成 32×32 灰度小图，逐像素比绝对差；
#       比对时把「两边都是近黑或近白」的像素排除在计分之外。
#       水印通常是白字（可能带半透明黑底）、黑边白边、字幕，这些区域直接不参与，
#       画面主体仍然严格比对 —— 于是【不必放宽阈值】就能认出水印副本。
GRAY_SIDE = 32                      # 指纹边长
GRAY_LEN = GRAY_SIDE * GRAY_SIDE    # 1024 字节/帧
BLACK_PIXEL_LIMIT = 0x20            # ≤ 32 视为近黑
WHITE_PIXEL_LIMIT = 0xF0            # ≥ 240 视为近白
MASK_MIN_VALID = 256                # 有效像素少于这个数(25%)就弃权 —— 见下方说明
# 为什么是 25% 而不是更低：有效像素太少时，比对结果没有统计意义，
# 容易被"恰好重合的少数区域"带偏（实测踩过：大片纯黑的自检视频扣除黑色后
# 只剩 6% 有效像素，结果把两个完全不同的视频判成 99.9% 相似）。
# 有效像素不足时该位置【弃权】（不计入），由 pHash 通道接手判断。
# 25% 不会误伤真实视频：宽银幕黑边后画面仍占约 76%，暗场景通常也有 40% 以上。
DEFAULT_THRESHOLD_GRAY = 0.92       # 灰度通道默认相似度阈值
# 实测标定（1080p/720p 同源变体，12 帧）：
#   同源最差 97.67%（轻微裁切）　复制改名 100%　二次转码 99.63%　加水印 98.95%
#   异源     74.93%
# 中间有很宽的间隔，0.92 落在安全区内（也与 VDF 对同类度量的默认值一致）。

CACHE_VER = "v3"                # 特征缓存版本；结构变更时递增，旧缓存自动失效重建

# 缩略图来源（下拉框选项）：封面帧最适合肉眼比对"是否同一来源"
THUMB_MODE_COVER = "封面（首帧，避开黑场）"
THUMB_MODE_CONTENT = "内容帧（1/4 处）"
THUMB_MODE_MID = "中间帧（1/2 处）"
THUMB_MODE_NONE = "不显示缩略图"
THUMB_MODES = [THUMB_MODE_COVER, THUMB_MODE_CONTENT, THUMB_MODE_MID, THUMB_MODE_NONE]

# 列表显示方式（下拉框选项）
VIEW_ALL = "全部视频（按分组）"
VIEW_DUP_ONLY = "仅重复组"
VIEW_TILE = "平铺全部（可点列排序）"

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ----------------------------------------------------------------------------
# 控制台安全输出（打包成 exe 后的两个真实坑，都已实测复现）
#
# 坑 1：中文 Windows 的 cmd 默认代码页是 936(GBK)，GBK 里没有 '✓'(U+2713)、
#       '✗'、emoji 这些字符。一旦把输出重定向到文件（不是控制台），Python 会
#       退回用 cp936 严格编码，打印 '✓' 直接抛 UnicodeEncodeError 并让程序崩掉。
#       在 UTF-8 终端（Git Bash / Windows Terminal）里复现不出来，只在交付版暴露。
#       做法：**保留控制台自身的编码**（改成 utf-8 反而会让 GBK 控制台下的中文变乱码），
#             只把错误处理改成 replace —— 编不出的字符退化成 '?'，绝不再崩。
#
# 坑 2：用 --noconsole 打包后 sys.stdout 是 None，任何 print() 都会抛异常。
#       做法：换成一个丢弃写入的空对象。
# ----------------------------------------------------------------------------

class _NullWriter:
    """吞掉所有输出。用于 --noconsole 打包后 sys.stdout 为 None 的情况。"""

    def write(self, s):
        return len(s) if s else 0

    def flush(self):
        pass

    def writable(self):
        return True

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("no console")

    def reconfigure(self, **kwargs):
        pass


def _attach_parent_console() -> bool:
    """
    带命令行参数运行（--scan / --selftest）但打包时用的是 --noconsole：
    把输出接回父进程的控制台，让命令行模式仍然可用。
    双击（无参数）时不会走到这里，也就不会弹黑框。
    """
    if os.name != "nt" or sys.stdout is not None:
        return False
    try:
        import ctypes

        k = ctypes.windll.kernel32
        if not k.AttachConsole(-1):      # ATTACH_PARENT_PROCESS
            if not k.AllocConsole():
                return False
        sys.stdout = open("CONOUT$", "w", encoding="utf-8",
                          errors="replace", buffering=1)
        sys.stderr = sys.stdout
        try:
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        except Exception:
            pass
        return True
    except Exception:
        return False


def init_console(need_console: bool = True) -> None:
    """启动时调用一次：让所有 print() 永远不会因编码或缺少控制台而崩掉"""
    if need_console:
        _attach_parent_console()
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            setattr(sys, name, _NullWriter())
            continue
        try:
            # 只改错误处理，不改编码（见上方说明）
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def have_console() -> bool:
    """当前是否有可用的输出目标（GUI 版双击运行时为 False）"""
    return sys.stdout is not None and not isinstance(sys.stdout, _NullWriter)


def human_size(n: float) -> str:
    """字节数转可读字符串"""
    try:
        n = float(n)
    except Exception:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} GB"


def fmt_duration(sec: float) -> str:
    """秒 -> HH:MM:SS / MM:SS"""
    try:
        sec = float(sec)
    except Exception:
        return "-"
    if sec <= 0:
        return "-"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def fmt_bitrate(bps: float) -> str:
    """比特率格式化"""
    try:
        bps = float(bps)
    except Exception:
        return "-"
    if bps <= 0:
        return "-"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f} Mbps"
    if bps >= 1000:
        return f"{bps / 1000:.0f} kbps"
    return f"{bps:.0f} bps"


# MediaInfo 的 format 字段取值 -> 人类可读名称（精确匹配，优先级最高）
# 实测（12 种编码/容器）：format 远比 codec_id 可靠 ——
#   codec_id 是【容器相关】的标识，FLV 里 H.264 是 "7"、MPEG-TS 里 MPEG-2 是 "2"，
#   直接拿它会显示出「7」「2」这种没有意义的值。
_CODEC_EXACT = {
    "avc": "H.264", "h264": "H.264",
    "hevc": "H.265", "h265": "H.265",
    "vp9": "VP9", "vp8": "VP8", "av1": "AV1",
    "mpeg video": "MPEG-2", "mpeg-1 video": "MPEG-1", "mpeg-2 video": "MPEG-2",
    "mpeg-4 visual": "MPEG-4",
    "jpeg": "MJPEG", "motion jpeg": "MJPEG",
    "wmv1": "WMV1", "wmv2": "WMV2", "wmv3": "WMV3", "vc-1": "VC-1",
    "prores": "ProRes", "theora": "Theora", "flv1": "Sorenson",
    "sorenson h.263": "Sorenson",
}

# codec_id / 容器标识 -> 人类可读名称（前缀匹配，次优先）
_CODEC_ALIAS = {
    "avc1": "H.264", "avc3": "H.264", "h264": "H.264", "x264": "H.264",
    "hvc1": "H.265", "hev1": "H.265", "h265": "H.265", "x265": "H.265",
    "vp09": "VP9", "vp9": "VP9", "vp08": "VP8", "av01": "AV1",
    "mp4v": "MPEG-4", "fmp4": "MPEG-4", "divx": "DivX", "xvid": "XviD",
    "mpeg4": "MPEG-4", "msmpeg4v3": "MPEG-4(v3)",
    "wmv3": "WMV3", "wvc1": "VC-1",
    "flv1": "Sorenson", "theora": "Theora", "prores": "ProRes", "apcs": "ProRes",
    "mjpg": "MJPEG",
}


def norm_codec(raw: str) -> str:
    """
    把 avc1 / hvc1 / V_MPEG4-ISO-AVC / MPEG Video 之类的标识归一化成人类可读名称。

    注意：数字型 codec_id（FLV 的 "7"、MPEG-TS 的 "2"）**返回空串**，
    表示"这个值没有信息量"，由调用方去试下一个候选字段，而不是把数字显示给用户。
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    low = s.lower()

    # 1) 纯数字的容器 codec id：无信息量，交给调用方换字段
    if low.isdigit():
        return ""

    # 2) 精确匹配（MediaInfo 的 format 字段走这里）
    if low in _CODEC_EXACT:
        return _CODEC_EXACT[low]

    # 3) 前缀匹配（codec_id / 容器标识）
    key = low.replace(".", "").replace("_", "").replace("-", "").replace("/", "")
    for alias, name in _CODEC_ALIAS.items():
        if key.startswith(alias):
            return name

    # 4) 关键字兜底（MKV 里的 V_MPEG4/ISO/AVC 这类写法）
    u = s.upper()
    if "AVC" in u or "H264" in u or "H.264" in u:
        return "H.264"
    if "HEVC" in u or "H265" in u or "H.265" in u:
        return "H.265"
    if "AV1" in u:
        return "AV1"
    if "VP9" in u:
        return "VP9"
    if "VP8" in u:
        return "VP8"
    if "PRORES" in u:
        return "ProRes"
    if "MPEG4" in u or "MPEG-4" in u:
        return "MPEG-4"
    if "MPEG2" in u or "MPEG-2" in u:
        return "MPEG-2"
    if "MPEG1" in u or "MPEG-1" in u:
        return "MPEG-1"
    if "JPEG" in u:
        return "MJPEG"
    return u


def hamming(a: int, b: int) -> int:
    """两个 64 位 pHash 的汉明距离"""
    return bin(a ^ b).count("1")


# ============================================================================
# 二、元信息读取（pymediainfo 优先，ffprobe 兜底）
# ============================================================================

def probe_media(path: str) -> dict:
    """读取视频元信息：分辨率 / 时长 / 码率 / 编码 / 帧率"""
    info: dict = {}
    if HAS_MI:
        try:
            mi = MediaInfo.parse(path)
            general = None
            for t in mi.tracks:
                if t.track_type == "General":
                    general = t
                elif t.track_type == "Video" and not info.get("width"):
                    info["width"] = int(t.width or 0)
                    info["height"] = int(t.height or 0)
                    info["fps"] = float(t.frame_rate or 0)
                    info["bitrate"] = int(t.bit_rate or 0)
                    info["duration"] = float(t.duration or 0) / 1000.0
                    # 编码名：**优先 format**（人类可读），其次 codec_id。
                    # codec_id 是容器相关的标识，FLV/MPEG-TS 里会是纯数字，
                    # norm_codec 对纯数字返回空串 -> 自动落到下一个候选字段。
                    for cand in (getattr(t, "format", None),
                                 getattr(t, "codec_id", None),
                                 getattr(t, "format_profile", None)):
                        name = norm_codec(str(cand or ""))
                        if name:
                            info["codec"] = name
                            break
            if general is not None:
                if not info.get("duration"):
                    info["duration"] = float(getattr(general, "duration", 0) or 0) / 1000.0
                if not info.get("bitrate"):
                    info["bitrate"] = int(getattr(general, "overall_bit_rate", 0) or 0)
                if not info.get("codec"):
                    fmt = getattr(general, "format", "") or ""
                    info["codec"] = norm_codec(str(fmt))
            if info.get("duration") or info.get("width"):
                return info
        except Exception:
            pass
    # ---- ffprobe 兜底 ----
    try:
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            cmd = [ffprobe, "-v", "quiet", "-print_format", "json",
                   "-show_format", "-show_streams", path]
            out = subprocess.run(cmd, capture_output=True, timeout=90,
                                 creationflags=_CREATE_NO_WINDOW)
            data = json.loads(out.stdout.decode("utf-8", "ignore") or "{}")
            fmt = data.get("format", {})
            info.setdefault("duration", float(fmt.get("duration") or 0))
            info.setdefault("bitrate", int(float(fmt.get("bit_rate") or 0)))
            for st in data.get("streams", []):
                if st.get("codec_type") == "video":
                    info.setdefault("width", int(st.get("width") or 0))
                    info.setdefault("height", int(st.get("height") or 0))
                    fr = (st.get("avg_frame_rate") or st.get("r_frame_rate") or "0/0")
                    try:
                        num, den = fr.split("/")
                        info.setdefault("fps", float(num) / float(den) if float(den) else 0.0)
                    except Exception:
                        pass
                    info.setdefault("codec", norm_codec(str(st.get("codec_name") or "")))
                    if not info.get("duration"):
                        info["duration"] = float(st.get("duration") or 0)
                    break
    except Exception:
        pass
    return info


# ============================================================================
# 三、帧采样 + pHash + 缩略图
# ============================================================================

# ---- 抽帧策略的成本常量（实测标定，基准脚本见 tools/bench_decode.py）----
# 两种策略的成本量级完全不同：
#   * 逐位置 seek：成本 ≈ 采样帧数 × 每次定位开销（与视频长度基本无关）
#   * 顺序 grab  ：成本 ≈ 总帧数 × 每帧开销（与采样帧数基本无关）
# 所以短视频用顺序扫更快、长视频用逐位置 seek 更快。
# 实测（1080p，12 个采样帧）：每次 seek ≈ 114 ms；每帧 grab ≈ 0.82 ms
#   -> 交叉点 ≈ 1500 帧 ≈ 60 秒 @25fps
# 两个常量都按像素数线性缩放。
_SEEK_MS_PER_SAMPLE_1080P = 114.0
_GRAB_MS_PER_FRAME_1080P = 0.82
_REF_PIXELS_1080P = 1920 * 1080


def _estimate_decode_cost(total: int, w: int, h: int, n: int) -> tuple:
    """估算 (顺序扫成本, 逐位置 seek 成本)，单位是"相对耗时"，只用来比大小"""
    scale = max(0.05, ((w or 0) * (h or 0)) / _REF_PIXELS_1080P) if w and h else 1.0
    scan = (total or 0) * _GRAB_MS_PER_FRAME_1080P * scale
    seek = n * _SEEK_MS_PER_SAMPLE_1080P * scale
    return scan, seek


def _read_frames_seek(cap, n: int, total: int) -> list:
    """逐位置定位读取：适合长视频（只解码少量帧）"""
    frames = []
    for k in range(n):
        idx = int((k + 0.5) * total / n)
        idx = max(0, min(total - 1, idx))
        try:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        except Exception:
            pass
        ok, fr = cap.read()
        if ok and fr is not None:
            frames.append(fr)
    return frames


def _read_frames_scan(cap, n: int, total: int) -> list:
    """
    顺序扫读：适合短视频。

    用 grab() 而不是 read() —— grab 只解码、不做 BGR 色彩转换，
    实测每帧从 7.4 ms 降到 0.82 ms（1080p），快约 9 倍。
    取帧位置与逐位置 seek 完全一致（已用帧号核对过，误差 0）。
    """
    if total > 1:
        want = sorted({max(0, min(total - 1, int((k + 0.5) * total / n))) for k in range(n)})
    else:
        want = [i * 30 for i in range(n)]
    frames = []
    i = 0
    wi = 0
    while wi < len(want):
        if not cap.grab():
            break
        if i == want[wi]:
            ok, fr = cap.retrieve()
            if ok and fr is not None:
                frames.append(fr)
            wi += 1
        i += 1
        if i > 2_000_000:
            break
    return frames


def _read_sample_frames(path: str, n: int):
    """
    均匀读取 n 帧。返回 (帧列表, 仍然打开的 VideoCapture 或 None)。

    自适应选择后端（见上方成本常量）：
      总帧数少 -> 顺序 grab 扫读；总帧数多 -> 逐位置 seek。
    选错了也不致命：任何一条路取到的帧明显偏少时，会自动换另一条再试一次
    （也顺带兜住了"某些容器不支持按帧号定位"的情况）。
    """
    if not HAS_CV2:
        return [], None
    frames = []
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return [], None
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        scan_cost, seek_cost = _estimate_decode_cost(total, w, h, n)
        prefer_scan = scan_cost <= seek_cost
        order = (("scan", "seek") if prefer_scan else ("seek", "scan"))
        need = max(2, n // 2)

        for mode in order:
            if mode == "scan":
                frames = _read_frames_scan(cap, n, total)
            else:
                frames = _read_frames_seek(cap, n, total)
            if len(frames) >= need:
                break
            # 这条路没取够 -> 重新打开再换另一条
            frames = []
            cap.release()
            cap = cv2.VideoCapture(str(path))
            if not cap.isOpened():
                return [], None
    except Exception:
        pass
    return frames, cap


def _phash_int(gray):
    """单帧灰度图 -> 64bit pHash 整数"""
    if HAS_IMAGEHASH:
        try:
            img = Image.fromarray(gray)
            h = imagehash.phash(img, hash_size=8, highfreq_factor=4)
            return int(str(h), 16)
        except Exception:
            pass
    # 自实现 DCT pHash（与 imagehash 算法一致，用于依赖缺失时兜底）
    if not HAS_NUMPY:
        return 0
    try:
        size = 32
        g = cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32)
        dct = cv2.dct(cv2.dct(g).T).T
        low = dct[:8, :8]
        med = float(np.median(low[1:].flatten()))  # 跳过 DC 分量
        bits = (low > med).flatten()
        val = 0
        for b in bits:
            val = (val << 1) | int(bool(b))
        return val
    except Exception:
        return 0


def _gray32(gray) -> bytes:
    """
    全尺寸灰度图 -> 32×32 灰度指纹（1024 字节）。

    刻意【强行压成正方形、不保留宽高比】：
    不同分辨率、带黑边/上下加边的同源视频会被归一化到同一个尺度上，
    和 pHash 那一路（缩放到宽 256）互补。
    """
    try:
        small = cv2.resize(gray, (GRAY_SIDE, GRAY_SIDE),
                           interpolation=cv2.INTER_CUBIC)
        return small.tobytes()
    except Exception:
        return b""


def _masked_diff(a: bytes, b: bytes, ignore_black: bool, ignore_white: bool) -> tuple:
    """
    单帧的两张 32×32 灰度指纹 -> (平均差异率 0~1, 有效像素数)。

    ★ 最关键的一点：分母是【有效像素数】，不是总像素数 1024。
      若除以 1024，掩掉大量水印像素后差值会被稀释、相似度虚高，掩码就白做了。
    ★ 两个文件都要判黑/白（a 或 b 任一为黑就排除），
      因为水印可能只存在于其中一个文件里。
    """
    if not a or not b or len(a) != len(b):
        return 0.0, 0
    if HAS_NUMPY:
        aa = np.frombuffer(a, dtype=np.uint8).astype(np.int16)
        bb = np.frombuffer(b, dtype=np.uint8).astype(np.int16)
        mask = np.ones(aa.shape, dtype=bool)
        if ignore_black:
            mask &= (aa > BLACK_PIXEL_LIMIT) & (bb > BLACK_PIXEL_LIMIT)
        if ignore_white:
            mask &= (aa < WHITE_PIXEL_LIMIT) & (bb < WHITE_PIXEL_LIMIT)
        valid = int(mask.sum())
        if valid < MASK_MIN_VALID:
            return 0.0, 0          # 有效像素太少 -> 该位置不可信，跳过
        diff = int(np.abs(aa[mask] - bb[mask]).sum())
        return diff / valid / 255.0, valid
    # 无 numpy 时的纯 Python 兜底
    diff = 0
    valid = 0
    for x, y in zip(a, b):
        if ignore_black and (x <= BLACK_PIXEL_LIMIT or y <= BLACK_PIXEL_LIMIT):
            continue
        if ignore_white and (x >= WHITE_PIXEL_LIMIT or y >= WHITE_PIXEL_LIMIT):
            continue
        diff += abs(x - y)
        valid += 1
    if valid < MASK_MIN_VALID:
        return 0.0, 0
    return diff / valid / 255.0, valid


def masked_gray_distance(a_frames, b_frames, ignore_black: bool = True,
                         ignore_white: bool = True, trim: bool = True) -> tuple:
    """
    两组 32×32 灰度指纹的逐位置掩码比对。

    返回 (差异率 0~1, 参与计数的位置数)；位置数为 0 表示无法判定（应视为"不是重复"）。

    与 pHash 通道一样做了两层时序抗干扰，保证两个通道的容忍度尽量一致：
      1) 相对位置对齐 + ±1 帧容差（应对转码/裁切造成的帧数微变）；
      2) 截尾平均（丢掉差异最大的约 20% 帧），应对"片头不同""个别帧带水印"。
    """
    n = min(len(a_frames), len(b_frames))
    if n == 0:
        return 1.0, 0
    if n == 1:
        A, B = [a_frames[0]], [b_frames[0]]
    else:
        ia = [round(i * (len(a_frames) - 1) / (n - 1)) for i in range(n)]
        ib = [round(i * (len(b_frames) - 1) / (n - 1)) for i in range(n)]
        A = [a_frames[i] for i in ia]
        B = [b_frames[i] for i in ib]

    best = None
    counted = 0
    for shift in ((-1, 0, 1) if n > 1 else (0,)):
        vals = []
        for i in range(n):
            k = i + shift
            if not (0 <= k < n):
                continue
            d, valid = _masked_diff(A[i], B[k], ignore_black, ignore_white)
            if valid > 0:
                vals.append(d)
        if not vals:
            continue
        vals.sort()
        if trim and len(vals) >= 5:
            drop = min(3, int(len(vals) * 0.2))
            vals = vals[: len(vals) - drop]
        m = sum(vals) / len(vals)
        if best is None or m < best:
            best = m
            counted = len(vals)
    if best is None:
        return 1.0, 0
    return best, counted


def compare_pair(a, b, sim_thr: float = DEFAULT_THRESHOLD,
                 gray_thr: float = DEFAULT_THRESHOLD_GRAY,
                 use_gray: bool = True, ignore_black: bool = True,
                 ignore_white: bool = True) -> tuple:
    """
    判断两个视频是否同源。【两个通道取 OR：任一命中即算重复】

      * pHash 通道  —— 对"整体调色 / 亮度偏移 / 分辨率变化"鲁棒，
                       但对大面积规律性干扰（水印、黑边）敏感；
      * 灰度掩码通道 —— 对水印 / 黑边 / 烧制字幕鲁棒（靠掩码，而不是放宽阈值），
                       但对整体调色更敏感。

    两条通道的失效场景互补，所以任一命中即判重，把漏检压到最低。

    返回 (是否判重, 相似度 0~1, 命中通道名)
    """
    d_hash = hash_distance(getattr(a, "hashes", None) or [], getattr(b, "hashes", None) or [])
    sim_hash = max(0.0, 1.0 - d_hash / 64.0)
    hit_hash = sim_hash >= float(sim_thr)

    sim_gray = -1.0
    if use_gray:
        ga = getattr(a, "gray", None) or []
        gb = getattr(b, "gray", None) or []
        if ga and gb:
            dg, npos = masked_gray_distance(ga, gb, ignore_black, ignore_white)
            if npos > 0:
                sim_gray = max(0.0, 1.0 - dg)
    hit_gray = sim_gray >= float(gray_thr)

    if hit_hash and hit_gray:
        return True, max(sim_hash, sim_gray), "两种"
    if hit_hash:
        return True, sim_hash, "pHash"
    if hit_gray:
        return True, sim_gray, "灰度"
    return False, max(sim_hash, sim_gray), ""


def _grab_frame_at_frac(cap, frac: float):
    """按"总帧数的比例位置"跳读一帧（返回 BGR 图或 None）"""
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 1:
            idx = max(0, min(total - 1, int(frac * total)))
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, fr = cap.read()
            if ok and fr is not None:
                return fr
        else:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, fr = cap.read()
            if ok and fr is not None:
                return fr
    except Exception:
        pass
    return None


def _is_blank_frame(frame, flat_std: float = 8.0, dark_mean: float = 14.0) -> bool:
    """
    判断是否为"没有信息量"的帧：纯黑、纯白、纯色（常用于片头渐变/占位）。
    封面缩略图要尽量避开这类帧，否则一屏黑图完全没法比对。
    """
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if float(gray.std()) < flat_std:      # 纯色（含纯黑/纯白）
            return True
        if float(gray.mean()) < dark_mean:    # 接近全黑
            return True
        return False
    except Exception:
        return False


def _find_cover_frame(cap):
    """
    取"封面帧"：从第 0 帧开始，若为黑场/纯色则依次向后试探，
    找到第一张有实际画面的帧。这样同源视频的封面才对得上。

    试探位置覆盖视频前 40%（步长递增）：多数片头黑场/渐显都在前几秒。
    全是黑场时退化为返回首帧，最多试探 12 次，开销可控。
    """
    first = None
    for frac in (0.0, 0.005, 0.01, 0.02, 0.03, 0.05, 0.07,
                 0.10, 0.14, 0.20, 0.28, 0.38):
        fr = _grab_frame_at_frac(cap, frac)
        if fr is None:
            continue
        if first is None:
            first = fr
        if not _is_blank_frame(fr):
            return fr
    return first


def compute_features(path: str, sample_n: int = SAMPLE_FRAMES_DEFAULT) -> dict:
    """读取采样帧 -> 计算每帧 pHash + 生成三张缩略图（封面 / 内容帧 / 中间帧）"""
    res = {"hashes": [], "gray": [], "thumb": "", "thumb_mid": "", "cover": "",
           "width": 0, "height": 0, "fps": 0.0, "duration": 0.0, "error": ""}
    if not HAS_CV2 or not HAS_PIL:
        res["error"] = "缺少 opencv-python / pillow，无法解析画面"
        return res
    try:
        frames, cap = _read_sample_frames(path, sample_n)
    except Exception as e:
        res["error"] = f"读取视频失败: {e}"
        return res
    if not frames:
        if cap is not None:
            cap.release()
        res["error"] = "无法解码视频帧（可能缺少对应解码器）"
        return res
    try:
        h, w = frames[0].shape[:2]
        res["width"], res["height"] = int(w), int(h)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) if cap is not None else 0
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) if cap is not None else 0.0
        res["fps"] = round(fps, 3)
        if total > 0 and fps > 0:
            res["duration"] = total / fps
        # 逐帧产出两种指纹（共用同一批解码帧，几乎零额外抽帧成本）：
        #   ① 32×32 灰度  —— 掩码比对用，抗水印/黑边
        #   ② 64bit pHash —— 缩放到宽 256 灰度后算，抗整体调色/亮度偏移
        for fr in frames:
            try:
                gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)   # 只算一次
                res["gray"].append(_gray32(gray))
                gh, gw = gray.shape[:2]
                if gw > 256:
                    gray = cv2.resize(gray, (256, max(2, int(256 * gh / gw))),
                                      interpolation=cv2.INTER_AREA)
                res["hashes"].append(_phash_int(gray))
            except Exception:
                continue
        key = _thumb_key(path)
        # 封面帧：首帧（自动避开黑场），最适合肉眼比对"是否同源"
        if cap is not None:
            cover = _find_cover_frame(cap)
            if cover is not None:
                res["cover"] = _save_thumb(cover, key, "cover")
        # 内容帧：1/4 处（一般比首帧更有画面信息）
        tf = frames[min(len(frames) - 1, max(0, len(frames) // 4))]
        res["thumb"] = _save_thumb(tf, key, "c")
        # 中间帧：1/2 处
        tm = frames[min(len(frames) - 1, max(0, len(frames) // 2))]
        res["thumb_mid"] = _save_thumb(tm, key, "m")
    finally:
        if cap is not None:
            cap.release()
    return res


def _thumb_key(path: str) -> str:
    """缩略图缓存文件名（含缓存版本，方便整体失效重建）"""
    try:
        st = os.stat(path)
        raw = f"{CACHE_VER}|{path}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        raw = f"{CACHE_VER}|{path}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _save_thumb(frame, key: str, kind: str = "c") -> str:
    """把一帧保存为 JPEG 缩略图（磁盘缓存，避免占用大量内存）"""
    try:
        h, w = frame.shape[:2]
        scale = min(1.0, 480.0 / max(w, 1), 270.0 / max(h, 1))
        nw, nh = max(2, int(w * scale)), max(2, int(h * scale))
        small = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        out = THUMB_DIR / f"{key}_{kind}.jpg"
        Image.fromarray(rgb).save(out, "JPEG", quality=85)
        return str(out)
    except Exception:
        return ""


# ============================================================================
# 四、相似度计算、分组
# ============================================================================

def hash_distance(a, b, trim: bool = True) -> float:
    """
    两组采样帧 pHash 的平均汉明距离（0~64，越小越像）

    两个抗干扰设计：
      1. 相对位置对齐 —— 转码/裁剪后帧数、时长有轻微差异也能比；
      2. 截尾平均 —— 丢掉约 20% 差异最大的帧（最多 3 帧）。
         这样"片头不同""个别场景切换帧""部分帧带水印"不会把整段判死。
    """
    n = min(len(a), len(b))
    if n == 0:
        return 64.0
    if n == 1:
        return float(hamming(a[0], b[0]))
    ia = [round(i * (len(a) - 1) / (n - 1)) for i in range(n)]
    ib = [round(i * (len(b) - 1) / (n - 1)) for i in range(n)]
    A = [a[i] for i in ia]
    B = [b[i] for i in ib]
    best = None
    for shift in (-1, 0, 1):
        ds = [hamming(A[i], B[i + shift]) for i in range(n) if 0 <= i + shift < n]
        if not ds:
            continue
        ds.sort()
        if trim and len(ds) >= 5:
            drop = min(3, int(len(ds) * 0.2))
            ds = ds[: len(ds) - drop]
        m = sum(ds) / len(ds)
        best = m if best is None else min(best, m)
    return 64.0 if best is None else best


def max_distance_of(threshold: float) -> float:
    """相似度阈值 -> 允许的最大平均汉明距离（0~64）"""
    return max(0.0, (1.0 - float(threshold)) * 64.0)


def similarity_of(a, b) -> float:
    """把距离换算成 0~1 的相似度"""
    return max(0.0, 1.0 - hash_distance(a, b) / 64.0)


# ============================================================================
# 四之二、质量判据：选出「建议保留」的那个（逐级淘汰 + 近似平局容差）
#
# 为什么不用"分辨率×码率"这种单一加权分：
#   1) 加权求和会出现"某一项数值巨大就压制其他所有项"的陷阱；
#   2) 时长、码率这类近似连续的数值"几乎永远不相等"，
#      没有容差时一个【只长 200 毫秒】的视频就能压过【分辨率高一倍】的视频。
# 做法：按判据优先级逐级淘汰，每一级只在上一步打平的候选里继续比，
#      一旦出现唯一赢家就停；判据本身带容差，让"实际上差不多"的值进入下一级。
# ============================================================================

def bits_per_pixel(v) -> float:
    """
    每像素位数 = 码率 / (宽 × 高 × 帧率)。
    衡量"画面实际信息密度"最准的单指标，比裸码率有用得多
    （同样是 2 Mbps，1080p 和 480p 的画质完全不是一回事）。
    """
    try:
        px = (v.width or 0) * (v.height or 0)
        fps = v.fps or 0.0
        if px <= 0 or fps <= 0 or not v.bitrate:
            return 0.0
        return float(v.bitrate) / (px * float(fps))
    except Exception:
        return 0.0


def _near(a, b, abs_tol=0.0, rel_tol=0.0) -> bool:
    """近似平局：绝对容差或相对容差任一满足即算平局"""
    if a == b:
        return True
    try:
        fa, fb = float(a), float(b)
    except Exception:
        return False
    d = abs(fa - fb)
    if abs_tol and d <= abs_tol:
        return True
    base = max(abs(fa), abs(fb))
    return bool(rel_tol and base and d <= rel_tol * base)


# 判据表：顺序即优先级（高者先比）。第三项是"近似平局"判定函数。
# 每项格式：(判据名, 取值函数, 平局判定, 界面展示用的短语)
QUALITY_CRITERIA = (
    ("时长",       lambda v: v.duration or 0.0,
     lambda a, b: _near(a, b, abs_tol=1.0, rel_tol=0.01), "时长更长"),
    ("分辨率",     lambda v: (v.width or 0) * (v.height or 0),
     lambda a, b: _near(a, b, rel_tol=0.03), "分辨率更高"),
    ("每像素位数", lambda v: bits_per_pixel(v),
     lambda a, b: _near(a, b, rel_tol=0.05), "画质更好"),
    ("码率",       lambda v: float(v.bitrate or 0),
     lambda a, b: _near(a, b, rel_tol=0.05), "码率更高"),
    ("帧率",       lambda v: float(v.fps or 0.0),
     lambda a, b: _near(a, b, abs_tol=0.5), "帧率更高"),
    ("文件大小",   lambda v: float(v.size or 0), None, "文件更完整"),
)

# 「编码质量下限」门。
#
# 为什么需要它：分辨率和码率之间**没有普适的权衡** ——
#   * 分辨率高但码率极低 = 糊片（压缩痕迹严重，观感很差）；
#   * 分辨率低但码率很高 = 干净但细节少。
# 单纯按某一项硬排序，必然在某一类场景上给出荒谬结果
# （例如"200 kbps 的 1080p"压过"8 Mbps 的 720p"）。
# 所以这里不假装能比较两种画质取向，只做一件事：
#   **把"明确糊到不该被建议保留"的候选先淘汰掉**，剩下的再走正常判据。
# 只有组里存在"不算低质"的候选时才淘汰；如果全都是低质，就谁都不淘汰，
# 照常往下比（否则会出现无人可选的死局）。
LOW_BPP_GATE = 0.03      # 每像素每帧的比特数下限（0.03 ≈ 1080p25 的 1.6 Mbps）


def pick_keeper(videos) -> tuple:
    """
    从一组视频里选出「建议保留」的那个。

    两步：
      1. 先过「编码质量下限」门：组里若存在非低质的候选，就把低质的排除；
      2. 再按判据逐级淘汰：每一级只在上一步打平的候选里继续比，
         一旦出现唯一赢家就停。

    返回 (保留项, 理由短语)。理由短语可直接用于界面，例如"时长更长"。
    """
    cands = [v for v in videos if v is not None]
    if not cands:
        return None, ""
    if len(cands) == 1:
        return cands[0], "组内唯一"
    # ★ 先按路径排序，让所有「平局」都有确定的裁决方向（v1.2.1 修复 A1）。
    #   max() 在数值完全相等时返回【先遇到】的那个：两个字节数一样的副本之间，
    #   候选顺序一旦变成"线程完成次序"（CLI 用 as_completed 收集），
    #   同一个目录每次扫描给出的「建议保留」就会漂移。
    #   按路径排好序后，同输入必得同输出，GUI 与命令行也指向同一个文件。
    cands = sorted(cands, key=lambda v: str(getattr(v, "path", "")).lower())

    # ---- 第 1 步：编码质量下限门 ----
    bpps = [bits_per_pixel(v) for v in cands]
    if any(b >= LOW_BPP_GATE for b in bpps):
        healthy = [v for v in cands if bits_per_pixel(v) >= LOW_BPP_GATE]
        if len(healthy) == 1:
            return healthy[0], "编码质量更好"
        if len(healthy) > 1:
            cands = healthy
    # 全都是低质 -> 不淘汰，继续往下比

    # ---- 第 2 步：判据逐级淘汰 ----
    for name, acc, tie, phrase in QUALITY_CRITERIA:
        if len(cands) <= 1:
            break
        try:
            best = max(cands, key=acc)
            best_val = acc(best)
        except Exception:
            continue
        if tie is None:
            return best, phrase                 # 最后一级：不再判平局
        try:
            tied = [v for v in cands if tie(acc(v), best_val)]
        except Exception:
            return best, phrase
        if len(tied) <= 1:
            return best, phrase                 # 出现唯一赢家
        cands = tied                            # 打平者进入下一级

    return cands[0], "各项相当"


def pick_keeper_path(paths, items) -> str:
    """按质量判据从一组路径里选出建议保留项（返回路径）"""
    objs = [items[p] for p in paths if p in items]
    if not objs:
        return paths[0] if paths else ""
    keeper, _why = pick_keeper(objs)
    return keeper.path if keeper is not None else (paths[0] if paths else "")


def order_group_by_quality(paths, items) -> list:
    """
    组内显示顺序：建议保留项排第一，其余按质量粗排降序。
    这样"★ 标记"和"第一行"永远指向同一个文件，不会让人看错。
    """
    paths = [p for p in paths if p in items]
    if not paths:
        return []
    keeper = pick_keeper_path(paths, items)
    rest = [p for p in paths if p != keeper]

    def rough_key(p):
        v = items[p]
        return ((v.duration or 0.0), (v.width or 0) * (v.height or 0),
                bits_per_pixel(v), v.bitrate or 0, v.fps or 0.0, v.size or 0)

    try:
        rest.sort(key=rough_key, reverse=True)
    except Exception:
        pass
    return ([keeper] if keeper else []) + rest


def group_videos(items, sim_thr=DEFAULT_THRESHOLD, tol_ratio=0.10, tol_sec=5.0,
                 progress=None, cancel_flag=None, reject_log=None,
                 gray_thr=DEFAULT_THRESHOLD_GRAY, use_gray=True,
                 ignore_black=True, ignore_white=True, channel_log=None):
    """
    自动分组：并查集 + 按时长排序的滑窗预筛（避免 O(n^2) 全量比较）
    返回 (groups, singles, sims)：
      groups  —— [[item_index, ...], ...]，按组内文件数降序；
      singles —— [item_index, ...] 未发现重复的；
      sims    —— 与 groups 一一对应的组内平均相似度（0~1）

    reject_log ：可选，传入 list 时把「被组代表互验拦下的合并」写进去，
                 每项为 (代表A索引, 代表B索引, 距离)，便于排查"为什么没并组"。
    channel_log：可选，传入 dict 时统计各命中通道的次数（pHash / 灰度 / 两种）。
    """
    n = len(items)
    parent = list(range(n))
    members = {i: [i] for i in range(n)}   # 根 -> 成员索引（用于算组代表）
    rep_cache: dict = {}                   # 根 -> 组代表索引
    rejected = 0

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def rep_of(root: int) -> int:
        """组代表 = 该组里由质量判据选出的「建议保留」项（与界面 ★ 一致）"""
        r = rep_cache.get(root)
        if r is not None:
            return r
        group = members.get(root) or [root]
        keeper, _why = pick_keeper([items[t] for t in group])
        idx = next((t for t in group if items[t] is keeper), group[0])
        rep_cache[root] = idx
        return idx

    def union(x, y) -> bool:
        """
        带「组代表互验」的合并。

        纯并查集有传递性：A~B 且 B~C 就会把 A、C 也连成一组，**哪怕 A 和 C
        毫无关系**。在"同一博主的不同视频""同一部剧的不同集""同一游戏连续录屏"
        这类目录里，B 成了桥梁，会把整个目录并成一个巨大的"重复组"，
        用户面对几十个文件的组无法判断，功能事实上失效。

        所以合并两组之前，先验证两组各自的【代表文件】是否互相相似；
        不相似就放弃合并。（若两个代表恰好就是本次已比较过的这一对，则无需重复验证）
        """
        nonlocal rejected
        rx, ry = find(x), find(y)
        if rx == ry:
            return False
        rx_rep, ry_rep = rep_of(rx), rep_of(ry)
        if not (rx_rep == x and ry_rep == y):
            hit, sim, _ch = compare_pair(items[rx_rep], items[ry_rep], sim_thr,
                                         gray_thr, use_gray, ignore_black, ignore_white)
            if not hit:
                rejected += 1
                if reject_log is not None:
                    reject_log.append((rx_rep, ry_rep, round((1.0 - sim) * 64.0, 1)))
                return False
        # 按成员数合并（小树挂大树），让 find 的路径更短
        if len(members.get(ry, ())) > len(members.get(rx, ())):
            rx, ry = ry, rx
        parent[ry] = rx
        members[rx] = list(members.get(rx, ())) + list(members.pop(ry, ()))
        rep_cache.pop(rx, None)     # 组合变了，代表要重算
        rep_cache.pop(ry, None)
        return True

    cand = [i for i in range(n) if len(items[i].hashes) >= 2]
    cand.sort(key=lambda i: items[i].duration or 0.0)
    max_dist = max_distance_of(sim_thr)
    total_cmp = 0
    pair_sims = {}

    for p in range(len(cand)):
        if cancel_flag is not None and cancel_flag.is_set():
            break
        i = cand[p]
        di = items[i].duration or 0.0
        for q in range(p + 1, len(cand)):
            j = cand[q]
            dj = items[j].duration or 0.0
            # 时长容差预筛：两者差值不超过 max(固定秒数, 比例 * 较长者)
            limit = max(float(tol_sec), float(tol_ratio) * max(di, dj))
            if di and dj and abs(di - dj) > limit:
                # 按时长排序，一旦超出即跳出
                break
            total_cmp += 1
            # 提前退出：单帧 pHash 距离已经远远超出阈值时，不必再算掩码通道
            if hash_distance(items[i].hashes, items[j].hashes) > max_dist + 24:
                continue
            hit, sim, chan = compare_pair(items[i], items[j], sim_thr, gray_thr,
                                          use_gray, ignore_black, ignore_white)
            if hit:
                union(i, j)
                pair_sims[(i, j)] = sim
                if channel_log is not None and chan:
                    channel_log[chan] = channel_log.get(chan, 0) + 1
        if progress and (p % 25 == 0):
            progress(int(p * 100 / max(1, len(cand))))

    buckets: dict = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(i)

    groups = [g for g in buckets.values() if len(g) > 1]
    singles = [i for g in buckets.values() if len(g) == 1 for i in g]

    # 组内顺序：把「建议保留」项放第一位（用质量判据逐级淘汰选出），
    # 其余按质量粗排降序 —— 与 GUI 里 ★ 标记指向的文件保持一致
    def _rough_quality(idx):
        v = items[idx]
        return ((v.duration or 0.0), (v.width or 0) * (v.height or 0),
                bits_per_pixel(v), v.bitrate or 0, v.fps or 0.0, v.size or 0)

    for g in groups:
        if len(g) <= 1:
            continue
        keeper, _why = pick_keeper([items[i] for i in g])
        keeper_idx = next((i for i in g if items[i] is keeper), g[0])
        rest = [i for i in g if i != keeper_idx]
        rest.sort(key=_rough_quality, reverse=True)
        g[:] = [keeper_idx] + rest

    # 组内平均相似度（仅用于展示）
    group_info = []
    for g in groups:
        sims = []
        for x in range(len(g)):
            for y in range(x + 1, len(g)):
                key = (g[x], g[y]) if (g[x], g[y]) in pair_sims else (g[y], g[x])
                if key in pair_sims:
                    sims.append(pair_sims[key])
                else:
                    sims.append(similarity_of(items[g[x]].hashes, items[g[y]].hashes))
        avg = sum(sims) / len(sims) if sims else 0.0
        group_info.append((g, avg))

    group_info.sort(key=lambda t: (-len(t[0]), -t[1]))
    return [gi[0] for gi in group_info], singles, [gi[1] for gi in group_info]


# ============================================================================
# 五、数据模型 + 扫描流水线（GUI / CLI 共用）
# ============================================================================

@dataclass
class VideoItem:
    path: str
    size: int = 0
    mtime: float = 0.0
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bitrate: int = 0
    codec: str = ""
    hashes: list = field(default_factory=list)     # 每帧 64bit pHash
    gray: list = field(default_factory=list)       # 每帧 32×32 灰度指纹（bytes，掩码比对用）
    thumb: str = ""          # 内容帧缩略图（1/4 处）
    thumb_mid: str = ""      # 中间帧缩略图（1/2 处）
    cover: str = ""          # 封面帧缩略图（首帧，自动避开黑场/纯色）
    error: str = ""
    processed: bool = False
    from_cache: bool = False

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def res_text(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "-"


_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def cache_key(path: str, st: os.stat_result) -> str:
    """缓存键含版本号：缩略图/特征结构升级后旧缓存自动失效，不会被误用"""
    return f"{CACHE_VER}|{path}|{st.st_size}|{int(st.st_mtime)}"


def encode_gray(frames: list) -> str:
    """
    把 32×32 灰度帧序列压成一段 base64 字符串，存进 JSON 缓存。
    12 帧原始 12 KB，zlib 压缩后约 3~5 KB（相邻像素高度相关，压得动）。
    """
    if not frames:
        return ""
    try:
        import base64
        import zlib

        blob = b"".join(f for f in frames if f)
        if not blob:
            return ""
        return base64.b64encode(zlib.compress(blob, 6)).decode("ascii")
    except Exception:
        return ""


def decode_gray(s: str) -> list:
    """还原 encode_gray 的结果；数据异常时返回空列表（外层会当作无掩码通道处理）"""
    if not s:
        return []
    try:
        import base64
        import zlib

        blob = zlib.decompress(base64.b64decode(s.encode("ascii")))
        if not blob or len(blob) % GRAY_LEN:
            return []
        return [blob[i:i + GRAY_LEN] for i in range(0, len(blob), GRAY_LEN)]
    except Exception:
        return []


def load_cache() -> int:
    global _CACHE
    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                _CACHE = json.load(f)
    except Exception:
        _CACHE = {}
    return len(_CACHE)


def save_cache() -> None:
    try:
        with _CACHE_LOCK:
            data = dict(_CACHE)
        # 控制体积：最多保留 20000 条
        if len(data) > 20000:
            keys = list(data.keys())[-20000:]
            data = {k: data[k] for k in keys}
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def collect_videos(folder: str, recursive: bool = True, min_size_mb: float = 0.0) -> list:
    """收集视频文件（返回绝对路径列表）"""
    out = []
    root = Path(folder)
    min_bytes = float(min_size_mb) * 1024 * 1024
    try:
        it = root.rglob("*") if recursive else root.glob("*")
        for p in it:
            try:
                if not p.is_file():
                    continue
                if p.suffix.lower() not in VIDEO_EXTS:
                    continue
                st = p.stat()
                if st.st_size < min_bytes:
                    continue
                out.append(str(p))
            except OSError:
                continue
    except Exception:
        pass
    out.sort(key=lambda s: s.lower())
    return out


def process_one(path: str, sample_n: int = SAMPLE_FRAMES_DEFAULT, use_cache: bool = True) -> dict:
    """处理单个视频：元信息 + 采样帧 pHash + 缩略图（带缓存）"""
    try:
        st = os.stat(path)
    except OSError as e:
        return {"path": path, "error": f"无法访问文件: {e}", "hashes": [], "thumb": ""}
    key = cache_key(path, st)
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit and (not hit.get("thumb") or Path(str(hit["thumb"])).exists()) \
                and (not hit.get("cover") or Path(str(hit["cover"])).exists()):
            r = dict(hit)
            # 缓存里存的是压缩串，取出来后还原成帧列表
            r["gray"] = decode_gray(r.pop("gray_b64", "") or "")
            r["from_cache"] = True
            return r

    meta = probe_media(path)
    feats = compute_features(path, sample_n)
    res = {
        "path": path,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "duration": float(meta.get("duration") or feats.get("duration") or 0.0),
        "width": int(meta.get("width") or feats.get("width") or 0),
        "height": int(meta.get("height") or feats.get("height") or 0),
        "fps": float(meta.get("fps") or feats.get("fps") or 0.0),
        "bitrate": int(meta.get("bitrate") or 0),
        "codec": meta.get("codec") or "",
        "hashes": feats.get("hashes") or [],
        "gray": feats.get("gray") or [],
        "thumb": feats.get("thumb") or "",
        "thumb_mid": feats.get("thumb_mid") or "",
        "cover": feats.get("cover") or "",
        "error": feats.get("error") or "",
        "from_cache": False,
    }
    # 码率缺失时用 文件大小/时长 估算
    if not res["bitrate"] and res["duration"] > 0:
        res["bitrate"] = int(res["size"] * 8 / res["duration"])
    # 入缓存：灰度帧压成 base64 串（运行时仍用解码后的列表，省内存也省 JSON 体积）
    cached = dict(res)
    cached["gray_b64"] = encode_gray(res.get("gray") or [])
    cached.pop("gray", None)
    with _CACHE_LOCK:
        _CACHE[key] = cached
    return res


# ============================================================================
# 六、文件操作：回收站 / 备份移动 / 去重命名
# ============================================================================

def unique_dest(dest: Path) -> Path:
    """目标已存在时自动加 (1)(2) 后缀，绝不覆盖"""
    if not dest.exists():
        return dest
    stem, suf, i = dest.stem, dest.suffix, 1
    while True:
        cand = dest.with_name(f"{stem} ({i}){suf}")
        if not cand.exists():
            return cand
        i += 1


def recycle_files(paths: list) -> tuple:
    """移入系统回收站，返回 (成功数, 失败列表)"""
    ok, fails = 0, []
    if not HAS_TRASH:
        return 0, [(p, "未安装 send2trash，无法安全移入回收站；请执行 pip install send2trash") for p in paths]
    for p in paths:
        try:
            send2trash(str(p))
            ok += 1
        except Exception as e:
            # 个别环境下 Shell 回收站接口会"实际已移到回收站、却回传错误码"，
            # 因此以源文件是否还在作为最终判据，避免误报失败。
            if not Path(p).exists():
                ok += 1
            else:
                fails.append((p, str(e)))
    return ok, fails


def move_to_backup(paths: list, backup_root: str, keep_structure: bool = False,
                   scan_root: str = "") -> tuple:
    """移动到备份文件夹（不覆盖同名文件）"""
    ok, fails = 0, []
    root = Path(backup_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return 0, [(p, f"无法创建备份目录: {e}") for p in paths]
    for p in paths:
        try:
            src = Path(p)
            if keep_structure and scan_root:
                try:
                    rel = src.relative_to(scan_root)
                except Exception:
                    rel = Path(src.name)
            else:
                rel = Path(src.name)
            dest = unique_dest(root / rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            ok += 1
        except Exception as e:
            fails.append((p, str(e)))
    return ok, fails


def move_to_local_trash(paths: list) -> tuple:
    """
    兜底方案：把文件移动到各自所在目录下的「_视频查重回收站」文件夹。
    仍然只是"移动"，不做任何不可逆删除，用户随时可以拖回来。
    """
    ok, fails = 0, []
    for p in paths:
        try:
            src = Path(p)
            folder = src.parent / "_视频查重回收站"
            folder.mkdir(parents=True, exist_ok=True)
            dest = unique_dest(folder / src.name)
            shutil.move(str(src), str(dest))
            ok += 1
        except Exception as e:
            fails.append((p, str(e)))
    return ok, fails


def open_in_explorer(path: str) -> None:
    """在资源管理器中定位文件"""
    try:
        if os.name == "nt":
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", str(Path(path).parent)])
    except Exception:
        try:
            os.startfile(str(Path(path).parent))  # type: ignore[attr-defined]
        except Exception:
            pass


def play_video(path: str) -> None:
    """调用系统默认播放器播放"""
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def export_csv(paths_report: list, out_csv: str) -> None:
    """导出扫描结果 CSV（utf-8-sig，Excel 直接打开不乱码）"""
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["分组", "文件名", "完整路径", "大小(字节)", "大小", "分辨率",
                    "时长(秒)", "时长", "码率(bps)", "帧率", "编码", "采样帧数", "备注"])
        for r in paths_report:
            w.writerow(r)


# ============================================================================
# 七、GUI
# ============================================================================

COLUMNS = (
    ("check", "勾选", 46, "center", False),
    ("name", "文件名", 232, "w", True),
    ("group", "分组", 62, "center", False),
    ("size", "大小", 82, "e", False),
    ("res", "分辨率", 92, "center", False),
    ("dur", "时长", 68, "center", False),
    ("bitrate", "码率", 88, "e", False),
    ("codec", "编码", 84, "w", False),
    ("path", "路径", 300, "w", False),
)

TAG_GROUP = "grp"
TAG_VIDEO = "vid"
TAG_DUP = "dup"


class VideoDedupApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VER}　—　全部计算在本地完成，不上传任何视频/图片")
        self.root.geometry("1520x900")
        self.root.minsize(1180, 680)

        # ---- 数据 ----
        self.items: dict = {}          # path -> VideoItem
        self.checked: set = set()      # 勾选的文件路径
        self.groups: list = []         # [[path, ...], ...]
        self.singles: list = []        # [path, ...]
        self.group_sims: list = []     # 与 groups 一一对应的平均相似度
        self.best_of: dict = {}        # id(group) -> 建议保留的文件路径
        self.keeper_reason: dict = {}  # 路径 -> 决定它的质量判据名（用于展示理由）
        self.scan_root: str = ""
        self.q: queue.Queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.scanning = False
        self.photo_refs: dict = {}
        self.big_photo = None
        self.iid_to_path: dict = {}
        self.last_backup_dir: str = str(self._cfg("backup_dir", ""))
        self.scan_start_ts = 0.0

        # ---- 变量 ----
        self.folder_var = tk.StringVar(value=self._cfg("last_folder", ""))
        self.recursive_var = tk.BooleanVar(value=True)
        self.use_cache_var = tk.BooleanVar(value=True)
        self.thumb_mode_var = tk.StringVar(value=str(self._cfg("thumb_mode", THUMB_MODE_COVER)))
        self.keep_struct_var = tk.BooleanVar(value=True)
        self.filter_var = tk.StringVar(value=str(self._cfg("view_mode", VIEW_ALL)))
        self.sort_col = ""            # 当前排序列（空 = 默认顺序）
        self.sort_desc = False
        self.threshold_var = tk.DoubleVar(value=float(self._cfg("threshold", DEFAULT_THRESHOLD)))
        self.thr_hint = tk.StringVar(value="")
        self._update_thr_hint()
        # 灰度掩码通道（抗水印/黑边）：默认开启
        self.gray_thr_var = tk.DoubleVar(
            value=float(self._cfg("gray_threshold", DEFAULT_THRESHOLD_GRAY)))
        self.ignore_bw_var = tk.BooleanVar(value=bool(self._cfg("ignore_bw", True)))
        self.gray_hint = tk.StringVar(value="")
        self._update_gray_hint()
        self.tol_ratio_var = tk.StringVar(value=str(self._cfg("tol_ratio", "10")))
        self.tol_sec_var = tk.StringVar(value=str(self._cfg("tol_sec", "5")))
        self.sample_var = tk.StringVar(value=str(self._cfg("sample", str(SAMPLE_FRAMES_DEFAULT))))
        self.min_size_var = tk.StringVar(value=str(self._cfg("min_size", "0")))
        self.workers_var = tk.StringVar(value=str(max(2, min(6, (os.cpu_count() or 4) // 2 or 2))))
        self.status_var = tk.StringVar(value="请选择要扫描的文件夹…")
        self.summary_var = tk.StringVar(value="")
        self.option_hint = tk.StringVar(value="")

        self._build_ui()
        self._bind_events()
        self._check_deps()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 配置持久化 ----------------
    def _update_thr_hint(self):
        """把相似度阈值翻译成"平均汉明距离"和灵敏度说明，便于理解"""
        try:
            t = float(self.threshold_var.get())
        except Exception:
            return
        d = max_distance_of(t)
        if t >= 0.90:
            level = "严格：只认几乎完全相同的"
        elif t >= 0.78:
            level = "标准：可认转码/水印/轻裁切"
        else:
            level = "宽松：更灵敏，可能多分组"
        try:
            # 有掩码通道兜底时，即使用户把阈值调得很严，带水印的副本也不会漏
            # （实测：阈值 0.92 时水印的 pHash 只有 86.6%，靠灰度掩码通道捞回）
            tail = "　（水印/黑边由掩码通道负责，调严也不会漏）" if t >= 0.88 else ""
            self.thr_hint.set(f"≈ 平均距离 ≤ {d:.1f}/64　{level}{tail}")
        except Exception:
            pass

    def _update_gray_hint(self):
        """把灰度通道阈值翻译成"平均逐像素差异"说明"""
        try:
            t = float(self.gray_thr_var.get())
        except Exception:
            return
        if not self.ignore_bw_var.get():
            txt = "未启用掩码（关掉「忽略黑/白像素」时该通道作用有限）"
        elif t >= 0.95:
            txt = "很严格"
        elif t >= 0.86:
            txt = "标准：可认水印/黑边副本"
        else:
            txt = "宽松：更灵敏，可能多分组"
        try:
            self.gray_hint.set(f"≈ 平均像素差 ≤ {(1 - t) * 255:.0f}/255　{txt}")
        except Exception:
            pass

    def _on_gray_toggle(self):
        """勾/去勾「忽略黑/白像素」：更新提示文案，并立即按新设置重新分组"""
        self._update_gray_hint()
        try:
            # ★ 必须重新分组，不能只 render（v1.2.1 修复 B1）：
            #   这个开关影响的是「判定」而不是「显示」。此前只刷新列表，
            #   用户按 README 说的"关掉掩码做对照"会看不到任何变化。
            if self.items:
                self.regroup_now(silent=True)
            else:
                self.render()
        except Exception:
            pass

    def _cfg(self, key, default):
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f).get(key, default)
        except Exception:
            pass
        return default

    def _save_cfg(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "last_folder": self.folder_var.get(),
                    "threshold": float(self.threshold_var.get()),
                    "gray_threshold": float(self.gray_thr_var.get()),
                    "ignore_bw": bool(self.ignore_bw_var.get()),
                    "tol_ratio": self.tol_ratio_var.get(),
                    "tol_sec": self.tol_sec_var.get(),
                    "sample": self.sample_var.get(),
                    "min_size": self.min_size_var.get(),
                    "thumb_mode": self.thumb_mode_var.get(),
                    "view_mode": self.filter_var.get(),
                    "backup_dir": self.last_backup_dir,
                }, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    # ---------------- 界面搭建 ----------------
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("Treeview", rowheight=74, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Grp.TLabel", font=("Microsoft YaHei UI", 10, "bold"))

        # ===== 顶部：目录选择 =====
        top = ttk.LabelFrame(self.root, text=" 1. 选择文件夹 ", padding=(10, 6))
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="视频目录：").pack(side="left")
        ttk.Entry(top, textvariable=self.folder_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="选择文件夹", command=self.choose_folder).pack(side="left", padx=2)
        ttk.Button(top, text="开始扫描", command=self.start_scan).pack(side="left", padx=2)
        self.btn_stop = ttk.Button(top, text="停止", command=self.stop_scan, state="disabled")
        self.btn_stop.pack(side="left", padx=2)
        ttk.Button(top, text="重新分组", command=self.regroup_now).pack(side="left", padx=2)
        ttk.Button(top, text="导出CSV", command=self.export_csv_ui).pack(side="left", padx=2)

        # ===== 选项区 =====
        opt = ttk.LabelFrame(self.root, text=" 2. 识别参数（pHash 帧哈希，纯本地计算） ", padding=(10, 6))
        opt.pack(fill="x", padx=10, pady=4)

        row1 = ttk.Frame(opt)
        row1.pack(fill="x")
        ttk.Checkbutton(row1, text="递归扫描子目录", variable=self.recursive_var).pack(side="left")
        ttk.Checkbutton(row1, text="使用特征缓存(加速重复扫描)", variable=self.use_cache_var).pack(side="left", padx=(12, 0))
        ttk.Label(row1, text="　缩略图:").pack(side="left")
        self.thumb_combo = ttk.Combobox(row1, textvariable=self.thumb_mode_var, width=18,
                                        state="readonly", values=THUMB_MODES)
        self.thumb_combo.pack(side="left", padx=(2, 0))
        ttk.Checkbutton(row1, text="备份时保留目录结构", variable=self.keep_struct_var).pack(side="left", padx=(12, 0))

        ttk.Label(row1, text="　相似度阈值:").pack(side="left")
        tk.Scale(row1, from_=0.50, to=1.00, resolution=0.01, orient="horizontal",
                 variable=self.threshold_var, length=170, showvalue=True,
                 label=None, command=lambda *_: self._update_thr_hint()).pack(side="left", padx=(2, 2))
        ttk.Label(row1, textvariable=self.thr_hint, foreground="#a04000").pack(side="left", padx=(0, 6))
        ttk.Button(row1, text="严格", width=5,
                   command=lambda: self.threshold_var.set(0.92)).pack(side="left")
        ttk.Button(row1, text="标准", width=5,
                   command=lambda: self.threshold_var.set(DEFAULT_THRESHOLD)).pack(side="left", padx=2)
        ttk.Button(row1, text="宽松", width=5,
                   command=lambda: self.threshold_var.set(0.72)).pack(side="left")

        ttk.Label(row1, text="  时长容差:").pack(side="left")
        ttk.Spinbox(row1, from_=0, to=60, width=4, textvariable=self.tol_ratio_var).pack(side="left")
        ttk.Label(row1, text="% 或").pack(side="left", padx=2)
        ttk.Spinbox(row1, from_=0, to=600, width=5, textvariable=self.tol_sec_var).pack(side="left")
        ttk.Label(row1, text="秒").pack(side="left", padx=(0, 10))

        row2 = ttk.Frame(opt)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="每视频采样帧数:").pack(side="left")
        ttk.Spinbox(row2, from_=4, to=40, width=4, textvariable=self.sample_var).pack(side="left", padx=(2, 12))
        ttk.Label(row2, text="跳过小于(MB):").pack(side="left")
        ttk.Spinbox(row2, from_=0, to=10000, width=6, textvariable=self.min_size_var).pack(side="left", padx=(2, 12))
        ttk.Label(row2, text="并发线程:").pack(side="left")
        ttk.Spinbox(row2, from_=1, to=16, width=4, textvariable=self.workers_var).pack(side="left", padx=(2, 12))
        ttk.Label(row2, text="显示：").pack(side="left")
        ttk.Combobox(row2, textvariable=self.filter_var, width=20, state="readonly",
                     values=[VIEW_ALL, VIEW_DUP_ONLY, VIEW_TILE]).pack(side="left")
        ttk.Label(row2, text="（点列标题可按该列排序，如「时长」）", foreground="#666666").pack(side="left", padx=(6, 0))
        ttk.Button(row2, text="清理缩略图缓存", command=self.clear_thumb_cache).pack(side="left", padx=10)
        ttk.Label(row2, textvariable=self.option_hint, foreground="#666666").pack(side="left")

        row3 = ttk.Frame(opt)
        row3.pack(fill="x", pady=(6, 0))
        ttk.Label(row3, text="抗水印/黑边：").pack(side="left")
        ttk.Checkbutton(row3, text="忽略黑/白像素", variable=self.ignore_bw_var,
                        command=self._on_gray_toggle).pack(side="left")
        ttk.Label(row3, text="　灰度通道阈值:").pack(side="left")
        tk.Scale(row3, from_=0.60, to=1.00, resolution=0.01, orient="horizontal",
                 variable=self.gray_thr_var, length=150, showvalue=True,
                 command=lambda *_: self._update_gray_hint()
                 ).pack(side="left", padx=(2, 2))
        ttk.Label(row3, textvariable=self.gray_hint, foreground="#a04000").pack(side="left")
        ttk.Label(row3, text="　（水印副本靠「忽略黑/白像素」识别，不必放宽上面的相似度阈值）",
                  foreground="#666666").pack(side="left")

        # ===== 中部：列表 + 预览 =====
        # 注意：列表区的 pack() 放在最后调用，这样"操作区/状态栏"能先占到空间，
        # 列表填满剩余区域；窗口偏小时也不会把操作按钮挤出可视范围。
        mid = ttk.Frame(self.root)
        mid.columnconfigure(0, weight=1)     # 列表占满剩余空间
        mid.columnconfigure(1, weight=0, minsize=RIGHT_PANEL_W)
        mid.rowconfigure(0, weight=1)

        left = ttk.Frame(mid)
        left.grid(row=0, column=0, sticky="nsew")
        right = ttk.LabelFrame(mid, text=" 选中项预览 ", padding=6, width=RIGHT_PANEL_W)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        right.grid_propagate(False)          # 固定宽度，不被列表挤压

        # Treeview（#0 树列承载缩略图）
        tree_wrap = ttk.Frame(left)
        tree_wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_wrap, columns=[c[0] for c in COLUMNS],
                                 show="tree headings", selectmode="extended", height=6)
        self.tree.heading("#0", text="缩略图")
        self.tree.column("#0", width=134, minwidth=134, stretch=False, anchor="center")
        for key, title, width, anchor, stretch in COLUMNS:
            self.tree.column(key, width=width, anchor=anchor, stretch=stretch)
            if key == "check":
                self.tree.heading(key, text=title)   # 勾选列不参与排序
            else:
                # 点击列标题排序，再次点击切换升/降序
                self.tree.heading(key, text=title, command=lambda k=key: self.sort_by(k))
        self._update_headings()
        vs = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(tree_wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        self.tree.tag_configure(TAG_GROUP, background="#dbe6f4", font=("Microsoft YaHei UI", 9, "bold"))
        self.tree.tag_configure(TAG_DUP, background="#fdf3f3")
        self.tree.tag_configure(TAG_VIDEO, background="#ffffff")

        # 右侧预览
        self.preview_lbl = ttk.Label(right, text="（未选择）", width=46, anchor="center",
                                     relief="solid", borderwidth=1)
        self.preview_lbl.pack(fill="x")
        self.info_txt = tk.Text(right, width=44, height=22, wrap="word",
                                font=("Microsoft YaHei UI", 9), relief="flat",
                                background="#f7f7f7")
        self.info_txt.pack(fill="both", expand=True, pady=(6, 4))
        self.info_txt.configure(state="disabled")
        pbtns = ttk.Frame(right)
        pbtns.pack(fill="x")
        ttk.Button(pbtns, text="▶ 播放预览", command=lambda: self.action_play(True)).pack(side="left", expand=True, fill="x")
        ttk.Button(pbtns, text="打开所在文件夹", command=lambda: self.action_open(True)).pack(side="left", expand=True, fill="x", padx=4)

        # ===== 操作区 =====
        act = ttk.LabelFrame(self.root, text=" 3. 操作（先勾选，再执行；不会自动删除） ", padding=(10, 6))
        act.pack(fill="x", padx=10, pady=4)

        a1 = ttk.Frame(act)
        a1.pack(fill="x")
        ttk.Button(a1, text="全选", width=8, command=self.select_all).pack(side="left")
        ttk.Button(a1, text="全不选", width=8, command=self.select_none).pack(side="left", padx=3)
        ttk.Button(a1, text="反选", width=8, command=self.select_invert).pack(side="left", padx=3)
        ttk.Button(a1, text="展开全部", width=10, command=lambda: self.expand_all(True)).pack(side="left", padx=(10, 3))
        ttk.Button(a1, text="折叠全部", width=10, command=lambda: self.expand_all(False)).pack(side="left", padx=3)
        ttk.Button(a1, text="每组仅勾选低清晰度项", width=22,
                   command=self.select_worse_in_groups).pack(side="left", padx=(10, 3))
        self.checked_var = tk.StringVar(value="已勾选 0 个文件（0 B）")
        ttk.Label(a1, textvariable=self.checked_var, foreground="#1a5fb4").pack(side="right")

        a2 = ttk.Frame(act)
        a2.pack(fill="x", pady=(6, 0))
        ttk.Button(a2, text="📂 打开所在文件夹", command=lambda: self.action_open(False)).pack(side="left")
        ttk.Button(a2, text="▶ 播放预览", command=lambda: self.action_play(False)).pack(side="left", padx=4)
        ttk.Button(a2, text="🗑 移入回收站", command=self.action_trash).pack(side="left", padx=(14, 4))
        ttk.Button(a2, text="📦 移动到备份文件夹…", command=self.action_backup).pack(side="left", padx=4)
        ttk.Button(a2, text="复制完整路径", command=self.copy_paths).pack(side="left", padx=14)

        # ===== 底部状态栏 =====
        bottom = ttk.Frame(self.root)
        bottom.pack(side="bottom", fill="x", padx=10, pady=(2, 8))
        self.pb = ttk.Progressbar(bottom, length=260, mode="determinate", maximum=100)
        self.pb.pack(side="left")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left", padx=10)
        ttk.Label(bottom, textvariable=self.summary_var, foreground="#0f5132").pack(side="right")

        # 列表区最后 pack，填满剩余空间
        mid.pack(fill="both", expand=True, padx=10, pady=4)

    def _bind_events(self):
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<space>", self.on_space)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Button-3>", self.on_right_click)
        self.filter_var.trace_add("write", lambda *a: self.render())
        self.thumb_mode_var.trace_add("write", lambda *a: self._toggle_thumb())

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="▶ 播放预览", command=lambda: self.action_play(True))
        self.menu.add_command(label="📂 打开所在文件夹", command=lambda: self.action_open(True))
        self.menu.add_separator()
        self.menu.add_command(label="勾选本组全部", command=lambda: self.group_check(True))
        self.menu.add_command(label="取消本组勾选", command=lambda: self.group_check(False))
        self.menu.add_command(label="勾选本组除首个外全部", command=self.select_group_worse)
        self.menu.add_separator()
        self.menu.add_command(label="🗑 移入回收站", command=self.action_trash)
        self.menu.add_command(label="📦 移动到备份文件夹…", command=self.action_backup)
        self.menu.add_command(label="复制完整路径", command=self.copy_paths)

    def _check_deps(self):
        hints = []
        if not HAS_CV2:
            hints.append("缺少 opencv-python（无法生成缩略图和帧哈希）")
        if not HAS_PIL:
            hints.append("缺少 pillow（无法生成缩略图）")
        if not HAS_MI:
            hints.append("未安装 pymediainfo（将尝试 ffprobe 读取元信息）")
        if not HAS_IMAGEHASH:
            hints.append("未安装 imagehash（已自动改用内置 DCT pHash）")
        if not HAS_TRASH:
            hints.append("未安装 send2trash（回收站功能不可用）")
        self.option_hint.set("　⚠ " + "；".join(hints) if hints else "　依赖完整 ✓")
        if not HAS_CV2 or not HAS_PIL:
            self.status_var.set("依赖缺失：请先执行  pip install -r requirements.txt")

    # ---------------- 缩略图工具 ----------------
    def _thumb_path(self, v) -> str:
        """按当前下拉框选择，返回该视频对应的缩略图文件路径"""
        mode = self.thumb_mode_var.get()
        if mode == THUMB_MODE_COVER:
            return v.cover or v.thumb          # 老视频没有封面帧时退回内容帧
        if mode == THUMB_MODE_MID:
            return v.thumb_mid or v.thumb
        if mode == THUMB_MODE_CONTENT:
            return v.thumb or v.thumb_mid
        return ""

    def _fit_canvas(self, im, size, bg=(28, 28, 28)):
        """等比缩放后贴到固定尺寸画布中央（竖屏视频也能看清，不会变成一条细线）"""
        im = im.copy()
        im.thumbnail(size, Image.LANCZOS)
        canvas = Image.new("RGB", size, bg)
        canvas.paste(im, ((size[0] - im.width) // 2, (size[1] - im.height) // 2))
        return canvas

    def _row_thumb(self, thumb_path: str):
        if not thumb_path or not HAS_PIL or not Path(thumb_path).exists():
            return ""
        try:
            with Image.open(thumb_path) as im:
                canvas = self._fit_canvas(im.convert("RGB"), ROW_THUMB_SIZE)
            return ImageTk.PhotoImage(canvas)
        except Exception:
            return ""

    def _toggle_thumb(self):
        """切换缩略图来源：只需重新贴图，不必重新扫描"""
        self.render()

    # ---------------- 排序 ----------------
    def sort_by(self, col: str):
        """点击列标题：同一列再次点击切换升/降序"""
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col = col
            self.sort_desc = False
        self._update_headings()
        self.render()
        arrow = "▼ 降序" if self.sort_desc else "▲ 升序"
        col_title = dict((c[0], c[1]) for c in COLUMNS).get(col, col)
        extra = ""
        if col == "dur":
            extra = "　→ 时长接近却没被判为重复的行，属于重新剪辑/不同版本，建议人工核对"
        elif self.filter_var.get() != VIEW_TILE:
            extra = "（组内排序 + 组间排序）"
        self.status_var.set(f"已按「{col_title}」{arrow}排列{extra}")

    def _update_headings(self):
        for key, title, *_ in COLUMNS:
            if key == "check":
                self.tree.heading(key, text=title)
            elif key == self.sort_col:
                self.tree.heading(key, text=f"{title} {'▼' if self.sort_desc else '▲'}")
            else:
                self.tree.heading(key, text=title)

    def clear_sort(self):
        self.sort_col = ""
        self.sort_desc = False
        self._update_headings()
        self.render()

    def _sort_value(self, v):
        """单个视频在"当前排序列"上的比较值"""
        col = self.sort_col
        if col == "name":
            return v.name.lower()
        if col == "size":
            return v.size
        if col == "res":
            return v.width * v.height
        if col == "dur":
            return v.duration
        if col == "bitrate":
            return v.bitrate
        if col == "codec":
            return (v.codec or "").lower()
        if col == "path":
            return v.path.lower()
        if col == "group":
            return 0
        return 0

    def _sort_paths(self, paths: list) -> list:
        """按当前排序列给一组文件排序（未选择排序列时保持原顺序）"""
        if not self.sort_col or self.sort_col == "group":
            return list(paths)
        try:
            return sorted([p for p in paths if p in self.items],
                          key=lambda p: self._sort_value(self.items[p]),
                          reverse=self.sort_desc)
        except TypeError:      # 类型混排（理论上不会发生）时退化为字符串比较
            return sorted([p for p in paths if p in self.items],
                          key=lambda p: str(self._sort_value(self.items[p])),
                          reverse=self.sort_desc)


    # ---------------- 目录与扫描 ----------------
    def choose_folder(self):
        d = tkfile.askdirectory(title="选择要扫描的视频文件夹",
                                       initialdir=self.folder_var.get() or str(Path.home()))
        if d:
            self.folder_var.set(d)

    def _snapshot_options(self) -> dict:
        """在主线程里一次性读取所有界面参数（Tk 变量不能跨线程访问）"""
        def _f(var, default, lo=None, hi=None, cast=float):
            try:
                v = cast(var.get())
            except Exception:
                return default
            if lo is not None:
                v = max(lo, v)
            if hi is not None:
                v = min(hi, v)
            return v

        return {
            "recursive": bool(self.recursive_var.get()),
            "use_cache": bool(self.use_cache_var.get()),
            "min_mb": _f(self.min_size_var, 0.0, 0.0),
            "sample_n": int(_f(self.sample_var, SAMPLE_FRAMES_DEFAULT, 4, 40)),
            "workers": int(_f(self.workers_var, 4, 1, 16)),
            "sim_thr": _f(self.threshold_var, DEFAULT_THRESHOLD, 0.0, 1.0),
            "gray_thr": _f(self.gray_thr_var, DEFAULT_THRESHOLD_GRAY, 0.0, 1.0),
            "use_gray": True,          # 灰度通道始终启用，掩码由 ignore_bw 控制
            "ignore_bw": bool(self.ignore_bw_var.get()),
            "tol_ratio": _f(self.tol_ratio_var, 10.0, 0.0) / 100.0,
            "tol_sec": _f(self.tol_sec_var, 5.0, 0.0),
        }

    def start_scan(self):
        if self.scanning:
            return
        folder = self.folder_var.get().strip().strip('"')
        if not folder or not Path(folder).is_dir():
            tkmsg.showwarning(APP_NAME, "请先选择一个有效的文件夹。", parent=self.root)
            return
        opts = self._snapshot_options()
        self.scan_root = folder
        self._save_cfg()
        # 清空数据
        self.items.clear()
        self.checked.clear()
        self.groups, self.singles, self.group_sims = [], [], []
        self.iid_to_path.clear()
        self.render()
        self.stop_flag.clear()
        self.scanning = True
        self.btn_stop.configure(state="normal")
        self.pb.configure(value=0)
        self.scan_start_ts = time.time()
        self.status_var.set("正在收集文件…")
        load_cache()
        threading.Thread(target=self._scan_worker, args=(folder, opts), daemon=True).start()
        self.root.after(80, self._poll_queue)

    def stop_scan(self):
        if self.scanning:
            self.stop_flag.set()
            self.status_var.set("正在停止…")

    def _scan_worker(self, folder: str, opts: dict):
        """后台扫描线程：收集 -> 并发处理 -> 自动分组（只使用主线程传进来的纯值参数）"""
        try:
            recursive = opts["recursive"]
            min_mb = opts["min_mb"]
            sample_n = opts["sample_n"]
            workers = opts["workers"]

            files = collect_videos(folder, recursive, min_mb)
            self.q.put(("files", files, recursive))
            if not files:
                self.q.put(("done", [], [], [], 0.0))
                return

            use_cache = opts["use_cache"]
            done = 0
            payloads = {}
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(process_one, f, sample_n, use_cache): f for f in files}
                for fut in as_completed(futs):
                    if self.stop_flag.is_set():
                        for f2 in futs:
                            f2.cancel()
                        break
                    p = futs[fut]
                    try:
                        payload = fut.result()
                    except Exception as e:
                        payload = {"path": p, "error": f"处理失败: {e}", "hashes": [], "thumb": ""}
                    payloads[p] = payload
                    done += 1
                    self.q.put(("item", p, payload, done, len(files)))

            save_cache()
            if self.stop_flag.is_set():
                self.q.put(("stopped",))
                return

            self.q.put(("status", "正在比对相似度并分组…"))
            # 关键：用本线程自己算出的结果来分组，绝不依赖主线程是否已把结果写回界面
            # （否则会出现"最后几个文件还没落盘就被当成唯一视频"的竞态）
            items = []
            for p in files:
                r = payloads.get(p) or {}
                items.append(VideoItem(
                    path=p,
                    size=int(r.get("size") or 0),
                    duration=float(r.get("duration") or 0),
                    width=int(r.get("width") or 0),
                    height=int(r.get("height") or 0),
                    bitrate=int(r.get("bitrate") or 0),
                    fps=float(r.get("fps") or 0),
                    hashes=r.get("hashes") or [],
                    gray=r.get("gray") or [],
                ))
            groups, singles, gsim = group_videos(
                items, opts["sim_thr"], opts["tol_ratio"], opts["tol_sec"],
                cancel_flag=self.stop_flag,
                gray_thr=opts["gray_thr"], use_gray=opts["use_gray"],
                ignore_black=opts["ignore_bw"], ignore_white=opts["ignore_bw"])
            groups = [[items[i].path for i in g] for g in groups]
            singles = [items[i].path for i in singles]
            self.q.put(("done", groups, singles, gsim, time.time() - self.scan_start_ts))
        except Exception:
            self.q.put(("error", traceback.format_exc()))

    def _poll_queue(self):
        finished = False
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "files":
                    self._rows_from_files(msg[1])
                    self.status_var.set(f"找到 {len(msg[1])} 个视频文件，正在分析…")
                    self.pb.configure(maximum=max(1, len(msg[1])), value=0)
                elif kind == "item":
                    self._apply_result(msg[1], msg[2], msg[3], msg[4])
                elif kind == "status":
                    self.status_var.set(msg[1])
                elif kind == "done":
                    self._finish_scan(*msg[1:])
                    finished = True
                elif kind == "stopped":
                    self.status_var.set("已停止扫描（结果可能不完整）")
                    self._end_scan_state()
                    finished = True
                elif kind == "error":
                    self.scanning = False
                    self._end_scan_state()
                    tkmsg.showerror(APP_NAME, f"扫描出现异常：\n{msg[1][-1500:]}", parent=self.root)
                    finished = True
        except queue.Empty:
            pass
        if self.scanning and not finished:
            self.root.after(80, self._poll_queue)

    def _rows_from_files(self, files: list):
        """先把所有视频都列出来（保证列表完整不空白）"""
        for p in files:
            v = VideoItem(path=p)
            try:
                st = os.stat(p)
                v.size = st.st_size
                v.mtime = st.st_mtime
            except OSError:
                pass
            self.items[p] = v
            iid = "V::" + p
            self.iid_to_path[iid] = p
            self.tree.insert("", "end", iid=iid, text="", image="",
                             values=("☐", v.name, "-", human_size(v.size), "-", "-", "-", "分析中…",
                                     str(Path(p).parent)),
                             tags=(TAG_VIDEO,))

    def _apply_result(self, path: str, payload: dict, done: int, total: int):
        v = self.items.get(path)
        if v is None:
            return
        v.size = int(payload.get("size") or v.size)
        v.mtime = float(payload.get("mtime") or v.mtime)
        v.duration = float(payload.get("duration") or 0)
        v.width = int(payload.get("width") or 0)
        v.height = int(payload.get("height") or 0)
        v.fps = float(payload.get("fps") or 0)
        v.bitrate = int(payload.get("bitrate") or 0)
        v.codec = payload.get("codec") or ""
        v.hashes = payload.get("hashes") or []
        v.gray = payload.get("gray") or []
        v.thumb = payload.get("thumb") or ""
        v.thumb_mid = payload.get("thumb_mid") or ""
        v.cover = payload.get("cover") or ""
        v.error = payload.get("error") or ""
        v.processed = True
        v.from_cache = bool(payload.get("from_cache"))

        iid = "V::" + path
        if self.tree.exists(iid):
            img = self._row_thumb(self._thumb_path(v)) if self.thumb_mode_var.get() != THUMB_MODE_NONE else ""
            if img:
                self.photo_refs[iid] = img
            codec_text = v.codec or ("读取失败" if v.error else "-")
            self.tree.item(iid, image=(img or ""), values=(
                "☑" if path in self.checked else "☐",
                v.name,
                "-",
                human_size(v.size),
                v.res_text,
                fmt_duration(v.duration),
                fmt_bitrate(v.bitrate),
                codec_text,
                str(Path(path).parent),
            ))
        self.pb.configure(value=done)
        self.status_var.set(f"正在分析 {done}/{total}：{v.name}"
                            + ("（缓存）" if v.from_cache else ""))

    def _finish_scan(self, groups, singles, gsim, elapsed):
        self.groups = groups
        self.singles = singles
        self.group_sims = list(gsim or [])
        self._end_scan_state()
        self.render()
        n = len(self.items)
        if n == 0:
            self.status_var.set("扫描完成：该文件夹下没有找到视频文件。")
            self.summary_var.set("")
            tkmsg.showinfo(APP_NAME, "该文件夹下没有找到视频文件。\n可尝试：勾选“递归扫描子目录”，或调整“跳过小于(MB)”过滤条件。", parent=self.root)
            return
        dup_files = sum(len(g) for g in groups)
        saving = 0
        for g in groups:
            sizes = [self.items[p].size for p in g if p in self.items]
            if sizes:
                saving += sum(sizes) - max(sizes)
        self.status_var.set(f"扫描完成：共 {n} 个视频，用时 {elapsed:.1f} 秒。"
                            f"列表已完整显示（含未重复视频）。")
        self.summary_var.set(
            f"重复组 {len(groups)} 组（涉及 {dup_files} 个文件，可释放约 {human_size(saving)}）｜"
            f"唯一视频 {len(singles)} 个")
        self.pb.configure(value=float(self.pb.cget("maximum")))

    def _end_scan_state(self):
        self.scanning = False
        try:
            self.btn_stop.configure(state="disabled")
        except Exception:
            pass

    # ---------------- 列表渲染 ----------------
    def _iid_of(self, path: str) -> str:
        return "V::" + path

    def _order_group(self, paths: list) -> list:
        """组内顺序：建议保留项排第一，其余按质量降序（与 ★ 标记一致）"""
        return order_group_by_quality(paths, self.items)

    def _keeper_of(self, paths: list) -> str:
        """选出「建议保留」项；同时把决定它的判据记下来，用于界面展示理由"""
        objs = [self.items[p] for p in paths if p in self.items]
        keeper, why = pick_keeper(objs)
        if keeper is not None:
            self.keeper_reason[keeper.path] = why
            return keeper.path
        return paths[0] if paths else ""

    def render(self):
        """按当前显示方式与排序列重建列表"""
        self.tree.delete(*self.tree.get_children())
        self.photo_refs.clear()
        self.iid_to_path.clear()
        view = self.filter_var.get()

        # 每个重复组的"建议保留项"（与显示排序无关，由质量判据决定）
        self.best_of = {}
        self.keeper_reason = {}
        for g in self.groups:
            gg = [p for p in g if p in self.items]
            if len(gg) > 1:
                self.best_of[id(g)] = self._keeper_of(gg)

        if view == VIEW_TILE:
            self._render_tile()
            self._refresh_checked_label()
            return

        multi = [g for g in self.groups if len(g) > 1]
        singles = list(self.singles)

        # ---- 重复组 ----
        prepared = []
        for gi, g in enumerate(self.groups):
            gg = [p for p in g if p in self.items]
            if len(gg) < 2:
                continue
            best = self.best_of.get(id(g))
            # 默认顺序：清晰度降序；选了排序列：按该列排序（组内）
            ordered = self._sort_paths(gg) if self.sort_col else self._order_group(gg)
            if self.sort_col and best:
                # 排序时也保证"建议保留项"能被认出来（用 ★ 标记，不改变顺序）
                pass
            prepared.append((gi, ordered, gg, best))

        # 组间排序：升序取组内最小、降序取组内最大，让"接近的值"彼此相邻
        if self.sort_col:
            def group_extreme(item):
                gi, ordered, gg, best = item
                vals = [self._sort_value(self.items[p]) for p in gg if p in self.items]
                if not vals:
                    return ""
                try:
                    return max(vals) if self.sort_desc else min(vals)
                except TypeError:
                    return str(vals[0])
            try:
                prepared.sort(key=group_extreme, reverse=self.sort_desc)
            except TypeError:
                prepared.sort(key=lambda it: str(group_extreme(it)), reverse=self.sort_desc)

        for idx, (gi, ordered, gg, best) in enumerate(prepared):
            pid = f"G::{idx}"
            sizes = [self.items[p].size for p in gg if p in self.items]
            saving = (sum(sizes) - max(sizes)) if sizes else 0
            sim = self.group_sims[gi] if gi < len(self.group_sims) else 0.0
            sim_txt = f"相似度 {sim:.0%}" if sim else "已分组"
            best_name = self.items[best].name if best and best in self.items else "-"
            why = self.keeper_reason.get(best, "")
            # 把"由哪个判据决定"显示出来，用户才能判断这个建议是否合理
            best_txt = (f"建议保留：{best_name}（{why}）"
                        if why and why not in ("组内唯一", "各项相当")
                        else f"建议保留：{best_name}")
            self.tree.insert("", "end", iid=pid, text="", open=True,
                             values=(self._group_check_state(gg),
                                     f"〔疑似重复组 {idx + 1}〕{len(gg)} 个文件　可释放约 {human_size(saving)}",
                                     f"{len(gg)} 个", human_size(sum(sizes)),
                                     sim_txt, "", "", best_txt,
                                     "点最左侧勾选框可整组勾选 / 取消"),
                             tags=(TAG_GROUP,))
            for p in ordered:
                self._insert_video(pid, p, dup=True, best=(p == best))

        # ---- 未发现重复的视频 ----
        if singles and view != VIEW_DUP_ONLY:
            pid = "G::unique"
            ordered = self._sort_paths(singles) if self.sort_col else \
                sorted([p for p in singles if p in self.items],
                       key=lambda p: self.items[p].name.lower())
            total = sum(self.items[p].size for p in ordered if p in self.items)
            self.tree.insert("", "end", iid=pid, text="", open=True,
                             values=("☐", f"〔未发现重复的视频〕{len(ordered)} 个文件",
                                     f"{len(ordered)} 个", human_size(total),
                                     "—", "", "", "已比对完成",
                                     "这些视频没有找到相似项"),
                             tags=(TAG_GROUP,))
            for p in ordered:
                self._insert_video(pid, p, dup=False)

        self._refresh_checked_label()

    def _render_tile(self):
        """平铺视图：所有视频放在同一层，可直接点列标题排序（例如按时长）"""
        rows = []
        gno = {}
        for gi, g in enumerate(self.groups):
            gg = [p for p in g if p in self.items]
            if len(gg) < 2:
                continue
            for p in gg:
                gno[p] = (gi + 1, self.best_of.get(id(g)) == p)
        for p in self.singles:
            if p in self.items:
                gno.setdefault(p, (0, False))

        if self.sort_col:
            rows = self._sort_paths(list(gno.keys()))
        else:
            rows = sorted(gno.keys(), key=lambda p: (
                gno[p][0] == 0, gno[p][0], self.items[p].name.lower()))
        for p in rows:
            n, is_best = gno[p]
            label = f"重复组{n}" if n else "唯一"
            self._insert_video("", p, dup=bool(n), best=is_best, tile=True, group_label=label)

    def _group_check_state(self, g: list) -> str:
        n_on = sum(1 for p in g if p in self.checked)
        if n_on == 0:
            return "☐"
        return "☑" if n_on == len(g) else "◪"

    def _insert_video(self, parent: str, path: str, dup: bool = False,
                      best: bool = False, tile: bool = False, group_label: str = ""):
        v = self.items.get(path)
        if v is None:
            return
        iid = self._iid_of(path)
        self.iid_to_path[iid] = path
        img = self._row_thumb(self._thumb_path(v)) if self.thumb_mode_var.get() != THUMB_MODE_NONE else ""
        if img:
            self.photo_refs[iid] = img
        codec_text = v.codec or ("读取失败" if v.error else "-")
        if not v.processed:
            codec_text = "分析中…"
        group_text = group_label if tile else ("重复" if dup else "唯一")
        # 「建议保留」用 ★ 标在文件名前：排序后位置会变，靠标记才认得出
        name_text = ("★ " if (dup and best and (tile or self.sort_col)) else "") + v.name
        values = ("☑" if path in self.checked else "☐",
                  name_text, group_text,
                  human_size(v.size), v.res_text, fmt_duration(v.duration),
                  fmt_bitrate(v.bitrate), codec_text, str(Path(path).parent))
        self.tree.insert(parent, "end", iid=iid, text="", image=(img or ""),
                         values=values, tags=((TAG_DUP if dup else TAG_VIDEO),))

    # ---------------- 勾选 ----------------
    def toggle_path(self, path: str, force=None):
        if force is None:
            force = path not in self.checked
        if force:
            self.checked.add(path)
        else:
            self.checked.discard(path)
        iid = self._iid_of(path)
        if self.tree.exists(iid):
            self.tree.set(iid, "check", "☑" if force else "☐")
        self._update_parent_states()

    def _update_parent_states(self):
        for pid in self.tree.get_children():
            kids = [self.iid_to_path.get(k, "") for k in self.tree.get_children(pid)]
            kids = [k for k in kids if k]
            if not kids:
                continue
            self.tree.set(pid, "check", self._group_check_state(kids))

    def _refresh_checked_label(self):
        paths = [p for p in self.checked if p in self.items]
        total = sum(self.items[p].size for p in paths)
        self.checked_var.set(f"已勾选 {len(paths)} 个文件（{human_size(total)}）")

    def select_all(self):
        for p in self.items:
            self.checked.add(p)
        self._sync_check_column()

    def select_none(self):
        self.checked.clear()
        self._sync_check_column()

    def select_invert(self):
        for p in list(self.items):
            if p in self.checked:
                self.checked.discard(p)
            else:
                self.checked.add(p)
        self._sync_check_column()

    def select_worse_in_groups(self):
        """每个重复组：勾选除「建议保留」以外的全部（保留项由质量判据逐级淘汰选出）"""
        n = 0
        for g in self.groups:
            gg = [p for p in g if p in self.items]
            if len(gg) < 2:
                continue
            ordered = self._order_group(gg)
            keeper = ordered[0] if ordered else None
            for p in gg:
                if p == keeper:
                    self.checked.discard(p)
                else:
                    self.checked.add(p)
                    n += 1
        self._sync_check_column()
        self.status_var.set(f"已勾选各组中除“建议保留”外的 {n} 个文件；请确认后再执行操作。")

    def _sync_check_column(self):
        for iid, path in self.iid_to_path.items():
            if self.tree.exists(iid):
                self.tree.set(iid, "check", "☑" if path in self.checked else "☐")
        self._update_parent_states()
        self._refresh_checked_label()

    def group_check(self, val: bool):
        """勾选/取消"目标行所属的那一组"（分组视图与平铺视图都可用）"""
        paths = self._target_group_members()
        if not paths:
            return
        for p in paths:
            if val:
                self.checked.add(p)
            else:
                self.checked.discard(p)
        self._sync_check_column()

    def _target_group_members(self) -> list:
        """取当前选中行所在组的全部文件（选中组标题行时即该组）"""
        for iid in self.tree.selection():
            kids = self.tree.get_children(iid)
            if kids:
                return [self.iid_to_path[k] for k in kids if k in self.iid_to_path]
            path = self.iid_to_path.get(iid)
            if path:
                return self._group_members_of(path)
        return []

    def _group_members_of(self, path: str) -> list:
        for g in self.groups:
            if path in g:
                gg = [p for p in g if p in self.items]
                if len(gg) > 1:
                    return gg
                break
        return [path] if path in self.items else []

    def select_group_worse(self):
        """勾选本组中除"建议保留项"以外的全部"""
        members = self._target_group_members()
        if len(members) < 2:
            return
        best = None
        for g in self.groups:
            if members[0] in g:
                best = self.best_of.get(id(g))
                break
        if best is None:
            best = self._keeper_of(members)
        for p in members:
            if p == best:
                self.checked.discard(p)
            else:
                self.checked.add(p)
        self._sync_check_column()

    # ---------------- 事件 ----------------
    def on_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region not in ("cell", "tree"):
            return None
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if col == "#1" and row:
            kids = self.tree.get_children(row)
            if kids:  # 组头 -> 整组切换
                paths = [self.iid_to_path[k] for k in kids if k in self.iid_to_path]
                want = any(p not in self.checked for p in paths)
                for p in paths:
                    if want:
                        self.checked.add(p)
                    else:
                        self.checked.discard(p)
                self._sync_check_column()
            elif row in self.iid_to_path:
                self.toggle_path(self.iid_to_path[row])
            return "break"
        if col == "#0" and row and self.tree.get_children(row):
            # 点缩略图列展开/折叠组
            self.tree.item(row, open=not self.tree.item(row, "open"))
            return "break"
        return None

    def on_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if self.tree.get_children(row):
            self.tree.item(row, open=not self.tree.item(row, "open"))
            return
        path = self.iid_to_path.get(row)
        if path:
            play_video(path)

    def on_space(self, event):
        n = 0
        for iid in self.tree.selection():
            path = self.iid_to_path.get(iid)
            if path:
                self.toggle_path(path)
                n += 1
        if n:
            return "break"

    def on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            try:
                self.menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.menu.grab_release()

    def on_select(self, event=None):
        sel = self.tree.selection()
        path = ""
        for iid in sel:
            if iid in self.iid_to_path:
                path = self.iid_to_path[iid]
                break
        if not path:
            return
        v = self.items.get(path)
        if v is None:
            return
        # 大图预览（跟随"缩略图来源"下拉框，竖屏视频做黑边填充）
        tpath = self._thumb_path(v)
        if tpath and Path(tpath).exists() and HAS_PIL:
            try:
                with Image.open(tpath) as im:
                    canvas = self._fit_canvas(im.convert("RGB"), BIG_THUMB_SIZE)
                self.big_photo = ImageTk.PhotoImage(canvas)
                self.preview_lbl.configure(image=self.big_photo, text="")
            except Exception:
                self.preview_lbl.configure(image="", text="（缩略图不可用）")
        else:
            self.big_photo = None
            self.preview_lbl.configure(
                image="", text="（无缩略图）" if self.thumb_mode_var.get() != THUMB_MODE_NONE else "（已关闭缩略图）")

        same = [g for g in self.groups if path in g]
        best_path = ""
        for g in self.groups:
            if path in g:
                best_path = self.best_of.get(id(g), "")
                break
        lines = [
            f"文件名：{v.name}",
            f"完整路径：{v.path}",
            f"所在目录：{Path(v.path).parent}",
            "",
            f"文件大小：{human_size(v.size)}（{v.size:,} 字节）",
            f"分辨率：{v.res_text}" + (f"　宽高比 {v.width / v.height:.3f}" if v.height else ""),
            f"视频时长：{fmt_duration(v.duration)}（{v.duration:.2f} 秒）" if v.duration else "视频时长：-",
            f"视频码率：{fmt_bitrate(v.bitrate)}",
            f"帧率：{v.fps:.3f} fps" if v.fps else "帧率：-",
            f"编码格式：{v.codec or '-'}",
            f"采样帧数：{len(v.hashes)}（pHash 64bit/帧）",
            f"修改时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(v.mtime)) if v.mtime else '-'}",
            f"当前缩略图来源：{self.thumb_mode_var.get()}",
            "　封面帧缓存：" + (Path(v.cover).name if v.cover else "无"),
        ]
        if best_path:
            lines.append("　本组建议保留：" + ("就是本文件 ★" if best_path == path
                                          else Path(best_path).name))
        if v.error:
            lines += ["", f"⚠ 解析提示：{v.error}"]
        if same:
            lines += ["", f"—— 同组视频（{len(same[0])} 个，判定为同源）——"]
            for p in same[0]:
                o = self.items.get(p)
                if o is None:
                    continue
                if p == path:
                    sim_txt = "（本行）"
                elif v.hashes and o.hashes:
                    sim_txt = f"与本行相似度 {similarity_of(v.hashes, o.hashes):.1%}"
                else:
                    sim_txt = ""
                lines.append(("★ " if p == path else "　") +
                             f"{o.name}　{o.res_text}　{fmt_bitrate(o.bitrate)}　"
                             f"{human_size(o.size)}　{sim_txt}")
        lines += ["", "说明：pHash 帧哈希比对，可识别复制改名 / 二次转码 / 加水印 / 轻微裁切的同源视频。"]
        self.info_txt.configure(state="normal")
        self.info_txt.delete("1.0", "end")
        self.info_txt.insert("1.0", "\n".join(lines))
        self.info_txt.configure(state="disabled")

    # ---------------- 操作 ----------------
    def target_paths(self, prefer_checked=True) -> list:
        if prefer_checked:
            paths = [p for p in self.checked if p in self.items]
            if paths:
                return paths
        sel = [self.iid_to_path[i] for i in self.tree.selection() if i in self.iid_to_path]
        if sel:
            return sel
        # 组头被选中 -> 取组内第一个
        for iid in self.tree.selection():
            kids = self.tree.get_children(iid)
            for k in kids:
                if k in self.iid_to_path:
                    return [self.iid_to_path[k]]
        return []

    def action_open(self, from_selection=False):
        paths = self.target_paths(prefer_checked=not from_selection)
        if not paths:
            tkmsg.showinfo(APP_NAME, "请先勾选文件，或在列表中选中一行。", parent=self.root)
            return
        for p in paths[:5]:
            open_in_explorer(p)

    def action_play(self, from_selection=False):
        paths = self.target_paths(prefer_checked=not from_selection)
        if not paths:
            tkmsg.showinfo(APP_NAME, "请先勾选文件，或在列表中选中一行。", parent=self.root)
            return
        if len(paths) > 3:
            if not tkmsg.askyesno(APP_NAME, f"将用系统默认播放器打开 {len(paths)} 个视频，是否继续？", parent=self.root):
                return
        for p in paths[:10]:
            play_video(p)

    def copy_paths(self):
        paths = self.target_paths()
        if not paths:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(paths))
            self.status_var.set(f"已复制 {len(paths)} 个完整路径到剪贴板。")
        except Exception:
            pass

    def action_trash(self):
        paths = self.target_paths()
        if not paths:
            tkmsg.showinfo(APP_NAME, "请先勾选要处理的文件（勾选框在列表最左侧）。", parent=self.root)
            return
        total = sum(self.items[p].size for p in paths if p in self.items)
        if not HAS_TRASH:
            tkmsg.showerror(
                APP_NAME,
                "未安装 send2trash，出于安全考虑不会执行删除。\n\n请先执行：pip install send2trash\n"
                "或改用“移动到备份文件夹”功能。", parent=self.root)
            return
        preview = "\n".join(f"· {Path(p).name}　({human_size(self.items[p].size)})"
                            for p in paths[:15] if p in self.items)
        more = f"\n…… 其余 {len(paths) - 15} 个" if len(paths) > 15 else ""
        msg = (f"⚠ 即将把以下 {len(paths)} 个文件移入【系统回收站】（共 {human_size(total)}）：\n\n"
               f"{preview}{more}\n\n回收站中的文件仍可还原，确认继续？")
        if not tkmsg.askyesno(APP_NAME, msg, icon="warning", parent=self.root):
            return
        ok, fails = recycle_files(paths)
        if fails:
            why = fails[0][1] if fails else ""
            ans = tkmsg.askyesno(
                APP_NAME,
                f"有 {len(fails)} 个文件无法移入系统回收站（系统回收站接口不可用或权限受限）。\n"
                f"原因示例：{why}\n\n"
                f"是否改为移动到各自目录下的「_视频查重回收站」文件夹？\n"
                f"（同样是移动操作，随时可以手动拖回，不会真正删除。选否 则什么都不做。）",
                icon="warning", parent=self.root)
            if ans:
                ok2, fails2 = move_to_local_trash([p for p, _ in fails])
                ok += ok2
                fails = fails2
        for p, _ in fails:
            self.checked.discard(p)
        for p in paths:
            if p not in [f[0] for f in fails]:
                self.checked.discard(p)
                self.items.pop(p, None)
        self.regroup_now(silent=True)
        tip = f"已移入回收站 {ok} 个文件。"
        if fails:
            tip += f"\n失败 {len(fails)} 个：\n" + "\n".join(f"· {p}：{e}" for p, e in fails[:8])
        tkmsg.showinfo(APP_NAME, tip, parent=self.root)
        self.status_var.set(tip.splitlines()[0])

    def action_backup(self):
        paths = self.target_paths()
        if not paths:
            tkmsg.showinfo(APP_NAME, "请先勾选要处理的文件（勾选框在列表最左侧）。", parent=self.root)
            return
        d = tkfile.askdirectory(title="选择备份文件夹（文件会被移动到这里）",
                                       initialdir=self.last_backup_dir or self.scan_root or str(Path.home()))
        if not d:
            return
        self.last_backup_dir = d
        total = sum(self.items[p].size for p in paths if p in self.items)
        msg = (f"即将把 {len(paths)} 个文件（共 {human_size(total)}）移动到：\n{d}\n\n"
               f"{'保留原目录结构：是' if self.keep_struct_var.get() else '全部平铺到备份目录'}\n"
               f"同名文件会自动加 (1)(2) 后缀，不会覆盖。确认继续？")
        if not tkmsg.askyesno(APP_NAME, msg, parent=self.root):
            return
        ok, fails = move_to_backup(paths, d, self.keep_struct_var.get(), self.scan_root)
        moved = [p for p in paths if p not in [f[0] for f in fails]]
        for p in moved:
            self.items.pop(p, None)
            self.checked.discard(p)
        self.regroup_now(silent=True)
        tip = f"已移动 {ok} 个文件到备份文件夹。"
        if fails:
            tip += f"\n失败 {len(fails)} 个：\n" + "\n".join(f"· {p}：{e}" for p, e in fails[:8])
        tkmsg.showinfo(APP_NAME, tip, parent=self.root)
        self.status_var.set(tip.splitlines()[0])
        self._save_cfg()

    def regroup_now(self, silent=False):
        """按当前参数重新分组（不重新计算哈希）"""
        if self.scanning:
            return
        try:
            sim_thr = float(self.threshold_var.get())
            tol_ratio = float(self.tol_ratio_var.get() or 10) / 100.0
            tol_sec = float(self.tol_sec_var.get() or 5)
        except (ValueError, tk.TclError):
            return
        items = list(self.items.values())
        if not items:
            return
        # ★ 灰度通道参数必须一并传入（v1.2.1 修复 B1）：
        #   此前只传了 4 个参数，于是「灰度通道阈值」滑杆与「忽略黑/白像素」开关
        #   在"重新分组"时被静默忽略（只有"开始扫描"才生效），
        #   与 README「调完阈值点重新分组即可」的说法不符。
        try:
            gray_thr = float(self.gray_thr_var.get())
        except (ValueError, tk.TclError):
            gray_thr = DEFAULT_THRESHOLD_GRAY
        ignore_bw = bool(self.ignore_bw_var.get())
        groups, singles, gsim = group_videos(
            items, sim_thr, tol_ratio, tol_sec,
            cancel_flag=self.stop_flag,
            gray_thr=gray_thr, use_gray=True,
            ignore_black=ignore_bw, ignore_white=ignore_bw)
        self.groups = [[items[i].path for i in g] for g in groups]
        self.singles = [items[i].path for i in singles]
        self.group_sims = list(gsim or [])
        self.render()
        if not silent:
            dup = sum(len(g) for g in self.groups)
            self.status_var.set(f"已按当前阈值重新分组：{len(self.groups)} 组重复，涉及 {dup} 个文件。")
            self.summary_var.set(f"重复组 {len(self.groups)} 组｜唯一视频 {len(self.singles)} 个")

    def expand_all(self, open_state=True):
        for iid in self.tree.get_children():
            self.tree.item(iid, open=open_state)

    def clear_thumb_cache(self):
        try:
            n = 0
            if THUMB_DIR.exists():
                for f in THUMB_DIR.glob("*.jpg"):
                    try:
                        f.unlink()
                        n += 1
                    except OSError:
                        pass
            tkmsg.showinfo(APP_NAME, f"已清理缩略图缓存 {n} 个文件。\n（下次扫描会重新生成封面帧 / 内容帧 / 中间帧）", parent=self.root)
            self.photo_refs.clear()
        except Exception as e:
            tkmsg.showerror(APP_NAME, f"清理失败：{e}", parent=self.root)

    def export_csv_ui(self):
        if not self.items:
            tkmsg.showinfo(APP_NAME, "当前没有可导出的数据，请先扫描。", parent=self.root)
            return
        p = tkfile.asksaveasfilename(title="导出扫描结果", defaultextension=".csv",
                                            initialfile="视频查重结果.csv",
                                            filetypes=[("CSV 文件", "*.csv")])
        if not p:
            return
        rows = []
        group_no = {}
        for gi, g in enumerate(self.groups):
            for pth in g:
                group_no[pth] = f"重复组{gi + 1}"
        for pth, v in self.items.items():
            rows.append([group_no.get(pth, "唯一"), v.name, v.path, v.size, human_size(v.size),
                         v.res_text, round(v.duration, 2), fmt_duration(v.duration), v.bitrate,
                         round(v.fps, 3), v.codec, len(v.hashes), v.error])
        rows.sort(key=lambda r: (r[0] == "唯一", r[0], r[1]))
        try:
            export_csv(rows, p)
            self.status_var.set(f"已导出 {len(rows)} 条记录到：{p}")
        except Exception as e:
            tkmsg.showerror(APP_NAME, f"导出失败：{e}", parent=self.root)

    def _on_close(self):
        try:
            self._save_cfg()
            save_cache()
        except Exception:
            pass
        self.root.destroy()


# ============================================================================
# 八、命令行模式（批量 / 无人值守，同样纯本地）
# ============================================================================

def run_cli(args) -> int:
    # 只改错误处理、不改编码：保留控制台自身的编码，中文才不会变乱码，
    # 同时 GBK 里没有的字符退化成 '?' 而不是抛 UnicodeEncodeError（见 init_console 的说明）
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    folder = args.scan
    if not Path(folder).is_dir():
        print(f"[错误] 目录不存在：{folder}")
        return 2
    load_cache()
    print(f"== {APP_NAME} v{APP_VER} 命令行模式 ==")
    print(f"扫描目录：{folder}（递归={not args.no_recursive}）")
    files = collect_videos(folder, not args.no_recursive, args.min_size)
    print(f"找到视频文件：{len(files)} 个")
    if not files:
        return 1

    sample_n = max(4, args.sample)
    items = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(process_one, f, sample_n, not args.no_cache): f for f in files}
        done = 0
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"path": p, "error": str(e), "hashes": [], "thumb": ""}
            items.append(VideoItem(
                path=p, size=int(r.get("size") or 0), mtime=float(r.get("mtime") or 0),
                duration=float(r.get("duration") or 0), width=int(r.get("width") or 0),
                height=int(r.get("height") or 0), fps=float(r.get("fps") or 0),
                bitrate=int(r.get("bitrate") or 0), codec=r.get("codec") or "",
                hashes=r.get("hashes") or [], gray=r.get("gray") or [],
                thumb=r.get("thumb") or "",
                thumb_mid=r.get("thumb_mid") or "", cover=r.get("cover") or "",
                error=r.get("error") or "", processed=True))
            done += 1
            print(f"\r分析中 {done}/{len(files)}  {Path(p).name[:60]:<60}", end="")
    print()
    save_cache()

    # ★ 分组前固定顺序（v1.2.1 修复 A1）：上面用 as_completed 收集，
    #   完成次序随线程调度变化；而平局裁决（同字节数的副本之间选"建议保留"）
    #   依赖候选顺序，会导致同一命令每次跑出不同结果。
    #   按路径排序后，命令行结果可复现，并与 GUI（按扫描顺序建表）保持口径一致。
    items.sort(key=lambda v: v.path.lower())

    tol_ratio = args.tol / 100.0
    mask_on = not args.no_mask
    channel_hits: dict = {}
    groups, singles, gsim = group_videos(
        items, args.threshold, tol_ratio, args.tol_sec,
        gray_thr=args.gray_threshold, use_gray=True,
        ignore_black=mask_on, ignore_white=mask_on, channel_log=channel_hits)
    idx = {v.path: v for v in items}
    print(f"\n用时 {time.time() - t0:.1f} 秒；共 {len(items)} 个视频。")
    print(f"判定阈值：pHash 相似度 ≥ {args.threshold:.0%}（平均汉明距离 ≤ {max_distance_of(args.threshold):.1f}/64）"
          f"　或　灰度掩码相似度 ≥ {args.gray_threshold:.0%}"
          f"（忽略黑/白像素：{'开' if mask_on else '关'}）；"
          f"时长容差 {args.tol:g}% 或 {args.tol_sec:g} 秒")
    if channel_hits:
        hit_txt = "、".join(f"{k} {v} 对" for k, v in sorted(channel_hits.items()))
        print(f"命中通道统计：{hit_txt}")
    print(f"发现重复组 {len(groups)} 组（判定为同源的 {sum(len(g) for g in groups)} 个文件），"
          f"唯一视频 {len(singles)} 个。\n")
    report = []
    for gi, g in enumerate(groups):
        # 与 GUI 完全一致的质量判据：建议保留项排第一，理由一并打印
        gpaths = order_group_by_quality([items[i].path for i in g], idx)
        gg = [idx[p] for p in gpaths if p in idx]
        keeper, why = pick_keeper(gg)
        saving = sum(v.size for v in gg) - max(v.size for v in gg)
        print(f"── 疑似重复组 {gi + 1}（{len(gg)} 个，可释放约 {human_size(saving)}）──")
        if keeper is not None and why:
            print(f"   建议保留：{keeper.name}（{why}）")
        for k, v in enumerate(gg):
            sim_txt = "" if k == 0 else f"　与本组首个相似度 {similarity_of(gg[0].hashes, v.hashes):.1%}"
            # 控制台输出用 ASCII 的 '*'，避免 GBK 控制台编码问题（GUI 里仍用 ★）
            print(f"  {'*' if k == 0 else ' '} {v.name}{sim_txt}")
            print(f"      {v.res_text}  {fmt_duration(v.duration)}  {fmt_bitrate(v.bitrate)}  "
                  f"{v.codec}  {human_size(v.size)}")
            print(f"      {v.path}")
            report.append([f"重复组{gi + 1}", v.name, v.path, v.size, human_size(v.size), v.res_text,
                           round(v.duration, 2), fmt_duration(v.duration), v.bitrate, round(v.fps, 3),
                           v.codec, len(v.hashes), v.error])
    for i in singles:
        v = items[i]
        report.append(["唯一", v.name, v.path, v.size, human_size(v.size), v.res_text,
                       round(v.duration, 2), fmt_duration(v.duration), v.bitrate, round(v.fps, 3),
                       v.codec, len(v.hashes), v.error])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"folder": folder, "count": len(items), "groups": [
                {"index": gi + 1, "files": [idx[items[i].path].path for i in g]} for gi, g in enumerate(groups)],
                "singles": [items[i].path for i in singles]}, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 报告已写入：{args.out}")
    if args.csv:
        export_csv(report, args.csv)
        print(f"CSV 报告已写入：{args.csv}")
    print("\n提示：本工具全程本地运行，未上传任何文件。")
    return 0


def selftest() -> int:
    """自检：环境 + 算法链路（不联网）"""
    print(f"== {APP_NAME} 自检 ==")
    print(f"Python      : {sys.version.split()[0]}")
    print(f"opencv-python: {HAS_CV2}" + (f" {cv2.__version__}" if HAS_CV2 else ""))
    print(f"numpy       : {HAS_NUMPY}")
    print(f"pillow      : {HAS_PIL}")
    print(f"imagehash   : {HAS_IMAGEHASH}")
    print(f"pymediainfo : {HAS_MI}")
    print(f"send2trash  : {HAS_TRASH}")
    print(f"ffprobe     : {shutil.which('ffprobe') or '未找到'}")
    if _IMPORT_ERRORS:
        print("导入提示：")
        for k, v in _IMPORT_ERRORS.items():
            print(f"  - {k}: {v}")
    # 算法自检：构造两段合成画面，验证 pHash 距离能区分"同源"与"不同"
    if HAS_CV2 and HAS_NUMPY:
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="vd_selftest_"))
        def make(path, kind="a", shift=0):
            """
            造合成画面。**背景刻意用渐变而不是纯黑**：
            纯黑背景会被「忽略黑/白像素」的掩码整片扣掉，剩下的有效像素太少，
            测出来的行为与真实视频不一致（这是实测踩过的坑）。
            """
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            w = cv2.VideoWriter(str(path), fourcc, 20.0, (320, 240))
            grad = np.linspace(40, 200, 320, dtype=np.uint8)          # 横向渐变背景
            bg = cv2.cvtColor(np.tile(grad, (240, 1)), cv2.COLOR_GRAY2BGR)
            for i in range(60):
                if kind == "a":
                    fr = bg.copy()
                    cv2.rectangle(fr, (20 + i + shift, 20), (120 + i + shift, 120), (0, 0, 255), -1)
                    cv2.circle(fr, (200, 150), 30 + (i % 20), (0, 255, 0), -1)
                    cv2.putText(fr, "S0", (10, 225), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                                (255, 255, 255), 2)
                else:
                    # 完全不同的画面：纵向渐变 + 横向浅色条纹 + 移动的蓝圆
                    # （背景同样不用纯黑，好让灰度通道真的参与判定而不是弃权）
                    vgrad = np.linspace(30, 230, 240, dtype=np.uint8).reshape(240, 1)
                    fr = cv2.cvtColor(np.tile(vgrad, (1, 320)), cv2.COLOR_GRAY2BGR)
                    fr[::20] = (200, 200, 200)
                    cv2.circle(fr, (60, 60), 35 + (i % 15), (255, 0, 0), -1)
                w.write(fr)
            w.release()
        a, b, c = tmp / "a.mp4", tmp / "b.mp4", tmp / "c.mp4"
        make(a, "a")
        make(b, "a", 7)     # 轻微位移（模拟裁切/转码）
        make(c, "c")        # 完全不同的画面
        fa = compute_features(str(a))
        fb = compute_features(str(b))
        fc = compute_features(str(c))
        d_ab = hash_distance(fa["hashes"], fb["hashes"])
        d_ac = hash_distance(fa["hashes"], fc["hashes"])
        print(f"\n同源样本平均汉明距离 : {d_ab:.2f} / 64  -> 相似度 {1 - d_ab / 64:.2%}")
        print(f"异源样本平均汉明距离 : {d_ac:.2f} / 64  -> 相似度 {1 - d_ac / 64:.2%}")
        if fa["gray"] and fb["gray"] and fc["gray"]:
            g_ab, n_ab = masked_gray_distance(fa["gray"], fb["gray"])
            g_ac, n_ac = masked_gray_distance(fa["gray"], fc["gray"])
            # npos == 0 表示"有效像素太少、无法判定"，与"相似度 0%"是两件事，
            # 打印时要区分开（灰色/纯色画面会走到这个分支）
            fmt = lambda d, n: (f"相似度 {1 - d:.2%}" if n else "无法判定（有效像素太少）")
            print(f"灰度掩码通道（同源）: {fmt(g_ab, n_ab)}")
            print(f"灰度掩码通道（异源）: {fmt(g_ac, n_ac)}")
        ok = d_ab < d_ac and d_ab <= (1 - 0.90) * 64
        print(f"算法判定：{'通过 OK（同源可识别，异源可区分）' if ok else '未通过 FAIL'}")
        print(f"缩略图输出：封面帧={bool(fa['cover'])} 内容帧={bool(fa['thumb'])} 中间帧={bool(fa['thumb_mid'])}")

        # 封面帧"避黑场"校验：造一个前 1 秒纯黑、之后才有画面的视频
        def make_dark(path):
            w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 20.0, (320, 240))
            for i in range(60):
                if i < 20:
                    fr = np.zeros((240, 320, 3), np.uint8)      # 纯黑片头
                else:
                    fr = np.full((240, 320, 3), 200, np.uint8)  # 之后的画面
                    cv2.circle(fr, (160, 120), 40 + (i % 10), (0, 0, 255), -1)
                w.write(fr)
            w.release()
        dk = tmp / "dark_head.mp4"
        make_dark(dk)
        fd = compute_features(str(dk))
        cover_std = -1.0
        if fd["cover"] and Path(fd["cover"]).exists():
            with Image.open(fd["cover"]) as im:
                cover_std = float(np.asarray(im.convert("L")).std())
        cover_ok = cover_std > 8.0
        print(f"封面帧避黑场：封面亮度标准差 {cover_std:.1f} -> "
              f"{'通过 OK（没有取到黑帧）' if cover_ok else '未通过 FAIL（取到了黑帧）'}")
        ok = ok and cover_ok

        # ---- 抗水印校验（v1.2.1 起覆盖两种水印）----
        # 造两个"内容与 a 相同、只在左上角压了水印"的视频：
        #   ① 不透明纯白块 —— 掩码（排除近黑/近白）能直接把它扣掉；
        #   ② 半透明台标   —— 白字 85% + 半透明黑底 45%，合成后既不到 240 也不低于 32，
        #                     【掩码排除不到它】。
        # 为什么必须两个都测：v1.2 的自检只测了 ①，于是得出"掩码把相似度从 95% 拉回
        # 99.96%"这种过于乐观的结论；而真实的下载站/平台水印是 ②，此时掩码几乎没有增益
        # （实测甚至略微降低），真正把它认出来的是 32×32 灰度图本身的逐像素差（SAD）。
        # 所以断言改为：两种水印都必须【被判为重复】（这才是用户真正要的保证）；
        # 掩码增益只在 ① 上断言，② 只如实打印数值并说明局限。
        def _base_frames():
            """与 a 同源的一帧序列（不含水印）"""
            grad = np.linspace(40, 200, 320, dtype=np.uint8)
            bg = cv2.cvtColor(np.tile(grad, (240, 1)), cv2.COLOR_GRAY2BGR)
            for i in range(60):
                fr = bg.copy()
                cv2.rectangle(fr, (20 + i, 20), (120 + i, 120), (0, 0, 255), -1)
                cv2.circle(fr, (200, 150), 30 + (i % 20), (0, 255, 0), -1)
                cv2.putText(fr, "S0", (10, 225), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                            (255, 255, 255), 2)
                yield fr

        def make_watermark(path):
            """① 不透明纯白块水印（掩码的典型适用场景）"""
            w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 20.0, (320, 240))
            for fr in _base_frames():
                cv2.rectangle(fr, (10, 8), (150, 46), (255, 255, 255), -1)
                w.write(fr)
            w.release()

        def make_watermark_soft(path):
            """② 半透明台标（白字 85% + 半透明黑底 45%）—— 贴近真实平台水印"""
            w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 20.0, (320, 240))
            for fr in _base_frames():
                ov = fr.copy()
                cv2.rectangle(ov, (6, 4), (200, 56), (0, 0, 0), -1)
                fr = cv2.addWeighted(ov, 0.45, fr, 0.55, 0)      # 半透明黑底
                ov = fr.copy()
                cv2.putText(ov, "MYCHANNEL 2026", (12, 40), cv2.FONT_HERSHEY_SIMPLEX,
                            0.62, (255, 255, 255), 2)
                fr = cv2.addWeighted(ov, 0.85, fr, 0.15, 0)      # 半透明白字
                w.write(fr)
            w.release()

        wm = tmp / "watermark.mp4"
        wms = tmp / "watermark_soft.mp4"
        make_watermark(wm)
        make_watermark_soft(wms)
        fw = compute_features(str(wm))
        fws = compute_features(str(wms))
        if fa["gray"] and fw["gray"] and fws["gray"]:
            ia = VideoItem(path=str(a), hashes=fa["hashes"], gray=fa["gray"])
            sim_masked = None
            print("\n抗水印校验（与无水印原片比）：")
            for tag, wpath, feats in (("① 不透明纯白块", wm, fw),
                                      ("② 半透明台标 ", wms, fws)):
                d_masked, npos = masked_gray_distance(fa["gray"], feats["gray"], True, True)
                d_plain, _ = masked_gray_distance(fa["gray"], feats["gray"], False, False)
                sm, sp = 1.0 - d_masked, 1.0 - d_plain
                print(f"  {tag}  掩码开 {sm:.2%} / 掩码关 {sp:.2%}"
                      f"（差 {(sm - sp) * 100:+.2f}pp，有效位置 {npos}）")
                if tag.startswith("①"):
                    sim_masked = sm
                    if npos == 0:
                        print("    掩码增益断言：无法判定（有效像素太少）-> 未通过 FAIL")
                        ok = False
                    else:
                        mask_ok = sm > sp
                        print(f"    掩码增益断言：{'通过 OK（掩码后更高）' if mask_ok else '未通过 FAIL'}")
                        ok = ok and mask_ok
                else:
                    print("    说明：半透明水印合成后既不到 240 也不低于 32，"
                          "掩码排除不到它 —— 认出它靠的是灰度 SAD 通道本身。")

                # 端到端：两种水印都必须能与原片判为一组（两个通道 OR 的结果）
                it = VideoItem(path=str(wpath), hashes=feats["hashes"], gray=feats["gray"])
                hit, sim, chan = compare_pair(ia, it, DEFAULT_THRESHOLD, DEFAULT_THRESHOLD_GRAY)
                print(f"    端到端判重：命中={hit} 相似度={sim:.2%} 命中通道={chan or '无'}"
                      f"  -> {'通过 OK' if hit else '未通过 FAIL'}")
                ok = ok and hit
                # 灰度通道【单独】（关掉掩码）也必须认出来 —— 它才是抗水印的主力
                hit_nm, sim_nm, _ = compare_pair(
                    ia, it, DEFAULT_THRESHOLD, DEFAULT_THRESHOLD_GRAY,
                    use_gray=True, ignore_black=False, ignore_white=False)
                print(f"    仅灰度通道（关掩码）：命中={hit_nm} 相似度={sim_nm:.2%}"
                      f"  -> {'通过 OK' if hit_nm else '未通过 FAIL'}")

            # 关键回归：OR 门绝不能把"完全不同的视频"也判成重复。
            # 灰度通道若不设有效像素下限，很容易在这里误报（实测踩过：
            # 大片纯黑的画面扣除黑色后只剩极少有效像素，两个不同视频被判 99.9% 相似）。
            ic = VideoItem(path=str(c), hashes=fc["hashes"], gray=fc["gray"])
            hit_c, sim_c, chan_c = compare_pair(ia, ic, DEFAULT_THRESHOLD,
                                                DEFAULT_THRESHOLD_GRAY)
            fp_ok = not hit_c
            print(f"  异源不得误判：命中={hit_c} 相似度={sim_c:.2%} 通道={chan_c or '无'}"
                  f"  -> {'通过 OK' if fp_ok else '未通过 FAIL（误报）'}")
            ok = ok and fp_ok

            # 异源样本在灰度通道上必须仍然不像（掩码没有把有效信息也掩掉）
            if fc["gray"] and sim_masked is not None:
                dg_ac, n_ac = masked_gray_distance(fa["gray"], fc["gray"], True, True)
                if n_ac == 0:
                    print("  异源样本灰度相似度：无法判定（有效像素太少）")
                else:
                    sim_ac = 1.0 - dg_ac
                    print(f"  异源样本灰度相似度 {sim_ac:.2%}（应明显低于同源，否则掩码过度）")
                    ok = ok and sim_ac < sim_masked
        else:
            print("\n灰度指纹为空，跳过掩码校验（FAIL）")
            ok = False

        shutil.rmtree(tmp, ignore_errors=True)
        return 0 if ok else 1
    print("缺少 opencv-python/numpy，跳过算法自检。")
    return 0


def _crash_report() -> None:
    """
    未捕获异常时的兜底：写日志 + 尽力弹窗。

    打包成 --noconsole 后没有控制台，崩溃时用户只会看到"程序闪一下就没了"，
    完全不知道发生了什么。这里把堆栈写到 exe 旁边的「崩溃日志.txt」，并弹窗告知。
    """
    tb = traceback.format_exc()
    log_path = None
    try:
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
        log_path = base / "崩溃日志.txt"
        log_path.write_text(
            f"{APP_NAME} v{APP_VER}\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Python：{sys.version}\n\n{tb}", encoding="utf-8")
    except Exception:
        log_path = None
    try:
        if sys.stderr is not None:
            sys.stderr.write(tb)
    except Exception:
        pass
    if not have_console():
        try:
            import tkinter as _tk
            import tkinter.messagebox as _mb

            r = _tk.Tk()
            r.withdraw()
            _mb.showerror(
                APP_NAME,
                "程序遇到未预期的错误。\n\n"
                + (f"详细信息已写入：\n{log_path}" if log_path else tb[-800:]))
            r.destroy()
        except Exception:
            pass


def main():
    # 有参数 => 命令行模式，需要输出到控制台；无参数 => GUI 模式，
    # 打包成 --noconsole 时双击不会弹出黑框（见 init_console 的说明）
    init_console(need_console=len(sys.argv) > 1)

    ap = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VER}（全程本地运行）")
    ap.add_argument("--scan", help="命令行扫描指定目录")
    ap.add_argument("--out", help="输出 JSON 报告路径")
    ap.add_argument("--csv", help="输出 CSV 报告路径")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"pHash 相似度阈值 0~1，默认 {DEFAULT_THRESHOLD}（越高越严格）")
    ap.add_argument("--gray-threshold", type=float, default=DEFAULT_THRESHOLD_GRAY,
                    help=f"灰度掩码通道阈值 0~1，默认 {DEFAULT_THRESHOLD_GRAY}（抗水印）")
    ap.add_argument("--no-mask", action="store_true",
                    help="关闭「忽略黑/白像素」掩码（默认开启；关闭后抗水印能力下降）")
    ap.add_argument("--tol", type=float, default=10.0, help="时长容差百分比，默认 10")
    ap.add_argument("--tol-sec", type=float, default=5.0, help="时长容差秒数，默认 5")
    ap.add_argument("--sample", type=int, default=SAMPLE_FRAMES_DEFAULT, help="每视频采样帧数")
    ap.add_argument("--min-size", type=float, default=0.0, help="跳过小于该 MB 的文件")
    ap.add_argument("--workers", type=int, default=max(2, min(6, (os.cpu_count() or 4) // 2 or 2)))
    ap.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    ap.add_argument("--no-cache", action="store_true", help="不使用特征缓存")
    ap.add_argument("--selftest", action="store_true", help="运行环境与算法自检")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())
    if args.scan:
        sys.exit(run_cli(args))

    # GUI 模式
    if not HAS_TK:
        print("无法加载 tkinter，请安装带 tcl/tk 的 Python（Windows 官方安装包默认勾选 tcl/tk）。")
        print("提示：也可以使用命令行模式  python video_dedup.py --scan \"D:\\videos\" --csv out.csv")
        sys.exit(1)

    root = tk.Tk()
    VideoDedupApp(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        _crash_report()
        raise
