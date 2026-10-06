# -*- coding: utf-8 -*-
"""
构建发布用 zip：把项目文件打进 dist/videodedup-<版本>.zip
（顶层带版本目录，解压即可用）

用法：
    python tools/build_release_zip.py            :: 用脚本里的默认版本
    python tools/build_release_zip.py --version v1.3
"""
from __future__ import annotations

import argparse
import hashlib
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VERSION = "v1.2.2"
OUT_DIR = ROOT / "dist"

# 要打包的文件（相对 ROOT）——不含 .git / .workbuddy / 缓存 / dist 自身
FILES = [
    "video_dedup.py",
    "requirements.txt",
    "运行工具.bat",
    "安装依赖.bat",
    "打包exe.bat",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "docs/screenshot-cover.png",
    "docs/screenshot-duration-sort.png",
    "docs/PROJECT-STATUS.md",
    "tools/build_release_zip.py",
    "tools/make_release_zip.py",
    "tools/gh_release.py",
    "tools/make_testdata.py",
    "tools/gui_smoketest.py",
    "tools/bench_decode.py",
    "tools/bench_decode_verify.py",
]

READ_ME_FIRST = """怎么用（Windows）
==================================================
【最省事】直接下载 Releases 里的 VideoDedupTool-v1.2.2-win64.zip ——
          不需要装 Python，也不需要装 ffmpeg，所有依赖都已打进那个文件夹。
          解压后双击 VideoDedupTool\\VideoDedupTool.exe 即可（文件夹别拆开）。

【想用源码版】需要 Python 3.8 以上（官方安装包默认自带 tkinter）：
  1. 解压到任意目录（中文路径也支持）
  2. 双击「安装依赖.bat」  （只需一次，从 PyPI 下载 opencv-python 等库）
  3. 双击「运行工具.bat」

想自己打包成免安装版：双击「打包exe.bat」
想先验证环境是否正常：python video_dedup.py --selftest

命令行批量模式：
    python video_dedup.py --scan "D:\\视频库" --csv 报告.csv

本工具不需要 ffmpeg
==================================================
元信息来自 pymediainfo 自带的 MediaInfo，解码来自 OpenCV 内置的 FFmpeg。
实测可解 H.264 / H.265(8bit+10bit) / VP9 / AV1 / Xvid / MJPEG / WMV2 /
MPEG-2(TS) / FLV / ProRes 等 12 种编码容器，全部成功。

隐私说明
==================================================
本工具全程在本机运行，不联网、不上传任何视频或图片、不调用云端模型。
所有删除类操作都必须人工勾选并二次确认，默认走系统回收站。

杀软说明
==================================================
本软件为自制工具，未做代码签名；所有代码都在开源仓库里，可自行查阅核对。
若被杀软误报，把所在文件夹加进信任区即可。

详细说明见 README.md，界面截图见 docs/ 目录，
更新日志见 CHANGELOG.md，项目状态（已停止开发）见 docs/PROJECT-STATUS.md。
Copyright (c) 2026 seanfan   MIT License
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=DEFAULT_VERSION, help=f"版本号，默认 {DEFAULT_VERSION}")
    args = ap.parse_args()

    version = args.version
    pkg_name = f"videodedup-{version}"
    out_zip = OUT_DIR / f"{pkg_name}.zip"
    OUT_DIR.mkdir(exist_ok=True)

    missing = [f for f in FILES if not (ROOT / f).exists()]
    if missing:
        raise SystemExit(f"缺少文件：{missing}")

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in FILES:
            src = ROOT / rel
            arc = f"{pkg_name}/{rel.replace(os.sep, '/')}"
            zi = zipfile.ZipInfo.from_file(src, arc)
            zi.compress_type = zipfile.ZIP_DEFLATED
            # 固定时间戳，让重复打包结果可复现（便于校验）
            zi.date_time = (2026, 10, 5, 0, 0, 0)
            with open(src, "rb") as f:
                z.writestr(zi, f.read())
        z.writestr(f"{pkg_name}/先读我.txt", READ_ME_FIRST.encode("utf-8"))

    size = out_zip.stat().st_size
    sha = hashlib.sha256(out_zip.read_bytes()).hexdigest()
    print(f"已生成：{out_zip}")
    print(f"大小：{size:,} 字节（{size / 1024:.1f} KB）")
    print(f"SHA256：{sha}")
    print("\n压缩包内容：")
    with zipfile.ZipFile(out_zip) as z:
        for i in z.infolist():
            print(f"  {i.filename:<56} {i.file_size:>9,} 字节")
        bad = z.testzip()
        print("\n完整性校验：", "通过 OK" if bad is None else f"损坏 FAIL ({bad})")

    # 校验清单（v1.2.1 起）：把本次发布产物的 SHA256 落到 dist/SHA256SUMS.txt，
    # 便于在不打开 Release 页面的情况下独立核对分发文件是否被改动。
    lines = [f"{sha}  {out_zip.name}"]
    # v1.2.2 起 exe 改成 onedir 文件夹版分发，这里顺带把文件夹包也记上
    folder_zip = OUT_DIR / f"VideoDedupTool-{version}-win64.zip"
    if folder_zip.exists():
        lines.append(f"{hashlib.sha256(folder_zip.read_bytes()).hexdigest()}"
                     f"  {folder_zip.name}")
    sums = OUT_DIR / "SHA256SUMS.txt"
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n校验清单：{sums}")
    for ln in lines:
        print(f"  {ln}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
