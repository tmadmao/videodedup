# -*- coding: utf-8 -*-
"""
合成测试样本生成器（供自检 / 回归 / 基准使用）

生成三类样本，全部用 ffmpeg 的 lavfi 确定性图案，不联网、不依赖外部素材：
  1. 同源变体   —— 原始 / 复制改名 / 二次转码 / 加水印 / 轻微裁切 / 完全无关
                  用于验证「同源能识别、异源能区分」
  2. 多编码矩阵 —— 同一段画面用 12 种编码/容器封装
                  用于验证解码覆盖；这些文件【内容相同编码不同】，
                  因此期望被判为同一组（这本身就是"跨编码查重"的用例）
  3. 连环并组   —— A、C 画面完全不同，B 由 A/C 各半拼成，使 B 与两者都在阈值内
                  用于验证「组代表互验」能阻止 A~B~C 被并成一组
  4. 损坏文件   —— 可恢复损坏 / 完全不可恢复损坏，用于验证错误处理

用法：
    python tools/make_testdata.py                     :: 生成到 ./testdata
    python tools/make_testdata.py --out D:\\samples    :: 指定目录
    python tools/make_testdata.py --dump-media        :: 额外打印各文件的 MediaInfo 字段
    python tools/make_testdata.py --only codecs       :: 只生成某一组

依赖：ffmpeg / ffprobe（仅造样本用，本工具运行期不需要）
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def find_tool(name: str) -> str:
    """定位 ffmpeg / ffprobe：先查 PATH，再试常见安装位置"""
    p = shutil.which(name)
    if p:
        return p
    for cand in (
        rf"C:\ffmpeg\bin\{name}.exe",
        rf"C:\Program Files\ffmpeg\bin\{name}.exe",
        f"/usr/bin/{name}",
        f"/usr/local/bin/{name}",
    ):
        if os.path.exists(cand):
            return cand
    return name


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")

SRC_V = "testsrc2=size=1280x720:rate=25:duration=8"
SRC_A = "sine=frequency=440:duration=8"


def run(args, timeout=300):
    """跑一条 ffmpeg/ffprobe 命令，失败时把 stderr 尾部抛出来"""
    r = subprocess.run(args, capture_output=True, timeout=timeout,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"{args[0]} 失败: {r.stderr.decode('utf-8', 'ignore')[-200:]}")
    return r.stdout


def ff(*args, timeout=300):
    return run([FFMPEG, "-v", "error", "-y", *args], timeout=timeout)


def probe_duration(path) -> float:
    try:
        out = run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                   "-of", "csv=p=0", str(path)])
        return float(out.decode().strip())
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
def make_variants(out: Path) -> list:
    """同源变体：验证识别能力"""
    made = []
    base = out / "原始视频.mp4"
    if not base.exists():
        ff("-f", "lavfi", "-i", SRC_V, "-f", "lavfi", "-i", SRC_A,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(base))
    made.append(base)

    a = out / "子目录A"
    b = out / "子目录B"
    a.mkdir(exist_ok=True)
    b.mkdir(exist_ok=True)

    p = a / "同一视频-改个名字.mp4"
    if not p.exists():
        shutil.copy(base, p)
    made.append(p)

    p = a / "二次转码-低码率.mp4"
    if not p.exists():
        ff("-i", str(base), "-vf", "scale=854:480", "-c:v", "libx264",
           "-preset", "veryfast", "-crf", "32", "-c:a", "aac", "-b:a", "64k", str(p))
    made.append(p)

    p = b / "带水印版本.mkv"
    if not p.exists():
        font = r"C\:/Windows/Fonts/arial.ttf"
        ff("-i", str(base),
           "-vf", f"drawtext=fontfile='{font}':text='MYCHANNEL 2026':x=40:y=40:"
                  f"fontsize=56:fontcolor=white@0.85:box=1:boxcolor=black@0.45",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "copy", str(p))
    made.append(p)

    p = b / "轻微裁切.mp4"
    if not p.exists():
        ff("-i", str(base), "-vf", "crop=1200:676:40:22,scale=1280:720",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy", str(p))
    made.append(p)

    p = b / "完全不同的视频.mp4"
    if not p.exists():
        ff("-f", "lavfi", "-i", "smptebars=size=960x540:rate=25:duration=6",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
           "-pix_fmt", "yuv420p", str(p))
    made.append(p)
    return made


# ---------------------------------------------------------------------------
CODEC_CASES = [
    ("01_h264.mp4",       ["-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p"]),
    ("02_h265_8bit.mp4",  ["-c:v", "libx265", "-crf", "28", "-pix_fmt", "yuv420p"]),
    ("03_h265_10bit.mp4", ["-c:v", "libx265", "-crf", "28", "-pix_fmt", "yuv420p10le"]),
    ("04_h265_8bit.mkv",  ["-c:v", "libx265", "-crf", "28", "-pix_fmt", "yuv420p"]),
    ("05_vp9.webm",       ["-c:v", "libvpx-vp9", "-crf", "40", "-b:v", "0"]),
    ("06_av1.mp4",        ["-c:v", "libaom-av1", "-crf", "45", "-b:v", "0", "-cpu-used", "8"]),
    ("07_xvid.avi",       ["-c:v", "mpeg4", "-qscale:v", "5"]),
    ("08_mjpeg.avi",      ["-c:v", "mjpeg", "-qscale:v", "5"]),
    ("09_wmv.wmv",        ["-c:v", "wmv2", "-b:v", "500k"]),
    ("10_h264.flv",       ["-c:v", "libx264", "-crf", "26", "-pix_fmt", "yuv420p"]),
    ("11_mpeg2.ts",       ["-c:v", "mpeg2video", "-b:v", "800k"]),
    ("12_prores.mov",     ["-c:v", "prores", "-profile:v", "1"]),
]


def make_codecs(out: Path) -> list:
    """多编码矩阵：验证解码覆盖 + 跨编码查重"""
    made = []
    for name, vargs in CODEC_CASES:
        p = out / name
        if p.exists() and p.stat().st_size > 200:
            made.append(p)
            continue
        cmd = [FFMPEG, "-v", "error", "-y",
               "-f", "lavfi", "-i", "testsrc2=duration=2:size=320x240:rate=25",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
               *vargs, "-shortest"]
        if name.endswith(".flv"):
            cmd += ["-f", "flv"]
        cmd.append(str(p))
        try:
            run(cmd, timeout=240)
        except Exception as e:
            print(f"    [跳过] {name}: {e}")
            continue
        made.append(p)
    return made


# ---------------------------------------------------------------------------
def make_chain(out: Path) -> list:
    """连环并组样本：A、C 不同，B 为两者的拼接（与 A、C 都在阈值内）"""
    made = []
    a = out / "连环-甲.mp4"
    c = out / "连环-丙.mp4"
    b = out / "连环-乙.mp4"
    if not a.exists():
        # 刻意用 testsrc（不是 testsrc2）——避免与"原始视频"撞同一个测试图案，
        # 否则两者本来就像，连环样本就失去意义了
        ff("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=6",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
           "-pix_fmt", "yuv420p", str(a))
    if not c.exists():
        ff("-f", "lavfi", "-i", "mandelbrot=size=640x360:rate=25", "-t", "6",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
           "-pix_fmt", "yuv420p", str(c))
    if not b.exists():
        # 前 3 秒是甲、后 3 秒是丙 —— 与两者都有 50% 相同，构成"桥梁"
        ff("-i", str(a), "-i", str(c), "-filter_complex",
           "[0:v]trim=0:3,setpts=PTS-STARTPTS[v0];"
           "[1:v]trim=0:3,setpts=PTS-STARTPTS[v1];"
           "[v0][v1]concat=n=2:v=1:a=0[out]",
           "-map", "[out]", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "24", "-pix_fmt", "yuv420p", str(b))
    made += [a, b, c]
    return made


# ---------------------------------------------------------------------------
def make_corrupt(out: Path) -> list:
    """损坏文件：验证错误处理不会中断整轮扫描"""
    made = []
    src = out / "原始视频.mp4"
    if not src.exists():
        return made
    p = out / "可恢复损坏.mp4"
    if not p.exists():
        ff("-i", str(src), "-c", "copy", "-bsf:v", "noise=amount=20:dropamount=8", str(p))
    made.append(p)
    p = out / "完全损坏.mp4"
    if not p.exists():
        (out).mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(200_000))
    made.append(p)
    return made


# ---------------------------------------------------------------------------
def dump_media(files: list) -> None:
    """打印各视频的 MediaInfo 关键字段（用于核对编码名归一化是否正确）"""
    try:
        from pymediainfo import MediaInfo
    except Exception as e:
        print(f"  未安装 pymediainfo，跳过：{e}")
        return
    print(f"\n  {'文件':<24}{'format':<18}{'codec_id':<10}{'format_profile'}")
    print("  " + "-" * 76)
    for f in files:
        try:
            mi = MediaInfo.parse(str(f))
        except Exception as e:
            print(f"  {f.name:<24} 解析失败: {str(e)[:40]}")
            continue
        for t in mi.tracks:
            if t.track_type == "Video":
                print(f"  {f.name:<24}{str(getattr(t, 'format', '')):<18}"
                      f"{str(getattr(t, 'codec_id', '')):<10}"
                      f"{str(getattr(t, 'format_profile', ''))}")
                break


def main() -> int:
    ap = argparse.ArgumentParser(description="生成视频查重的合成测试样本")
    ap.add_argument("--out", default="testdata", help="输出目录（默认 ./testdata）")
    ap.add_argument("--only", choices=["variants", "codecs", "chain", "corrupt"],
                    help="只生成某一组")
    ap.add_argument("--dump-media", action="store_true",
                    help="额外打印 MediaInfo 字段（核对编码名归一化）")
    args = ap.parse_args()

    if not shutil.which(FFMPEG) and not os.path.exists(FFMPEG):
        print("找不到 ffmpeg，无法生成样本。请先安装 ffmpeg 并加入 PATH。")
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"ffmpeg : {FFMPEG}")
    print(f"输出到 : {out.resolve()}\n")

    want = {args.only} if args.only else {"variants", "codecs", "chain", "corrupt"}
    made = []
    if "variants" in want:
        print("[1/4] 同源变体（原始/改名/转码/水印/裁切/无关）...")
        made += make_variants(out)
    if "codecs" in want:
        print("[2/4] 多编码矩阵（12 种编码/容器）...")
        made += make_codecs(out)
    if "chain" in want:
        print("[3/4] 连环并组样本（甲/乙/丙）...")
        made += make_chain(out)
    if "corrupt" in want:
        print("[4/4] 损坏文件...")
        made += make_corrupt(out)

    print(f"\n完成，共 {len(made)} 个文件：")
    for f in made:
        if f.exists():
            print(f"  {str(f.relative_to(out)):<34}{f.stat().st_size:>10,} 字节"
                  f"  {probe_duration(f):.1f}s")

    if args.dump_media:
        dump_media([f for f in made if f.exists()])
    return 0


if __name__ == "__main__":
    sys.exit(main())
