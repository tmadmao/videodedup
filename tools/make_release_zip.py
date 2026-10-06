"""把 onedir 打包产物压成发布用的 zip。

为什么要 onedir + zip 这一套，而不是单文件 exe：
  * 单文件（``--onefile``）每次启动都要把 ~100MB 依赖解压到临时目录，
    双击后要等 5~10 秒；onedir 是解压好的文件夹，启动是瞬时的。
  * 单文件的自解压行为在个别杀软（启发式 ``HEUR/QVM*.Malware.Gen``）眼里
    与"释放载荷的木马"高度相似，容易被误报；
    onedir 不含自解压，误报面小得多。

用法::

    python tools/make_release_zip.py                 # 打包默认目录
    python tools/make_release_zip.py --name VideoDedupTool
    python tools/make_release_zip.py --version v1.2.3
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

READ_ME_FIRST = """怎么用（Windows）
==================================================
这是「文件夹版」，不是单文件 exe —— 文件夹和里面的文件要放在一起。

  1. 把整个 VideoDedupTool 文件夹解压/复制到任意位置（中文路径也支持）
  2. 双击文件夹里的 VideoDedupTool.exe

不需要装 Python，也不需要装 ffmpeg，所有依赖都已经在这个文件夹里。
启动是瞬时的（单文件版每次启动要先解压 ~100MB，要等 5~10 秒，所以没做成单文件）。

exe 可以自由改名，比如改成「视频查重工具.exe」，不影响运行。

命令行批量模式（在 cmd 里 cd 到本目录再执行）：
    VideoDedupTool.exe --scan "D:\\视频库" --csv 报告.csv
    VideoDedupTool.exe --selftest

隐私说明
==================================================
本工具全程在本地运行，不联网、不上传任何视频或图片、不调用云端模型。
所有删除类操作都必须人工勾选并二次确认，默认走系统回收站。

杀软说明
==================================================
本软件为自制工具，未做代码签名；所有代码都在开源仓库里，可自行查阅核对。
若被杀软误报，把本文件夹加进信任区即可。

开源仓库：https://github.com/tmadmao/videodedup
Copyright (c) 2026 seanfan   MIT License
"""


def detect_version() -> str:
    """从 video_dedup.py 里读 APP_VER，避免版本号写两处。"""
    try:
        text = (ROOT / "video_dedup.py").read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'^APP_VER\s*=\s*"([^"]+)"', text, re.M)
        if m:
            return "v" + m.group(1)
    except Exception:
        pass
    return "v0.0.0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="VideoDedupTool")
    ap.add_argument("--version", default="")
    args = ap.parse_args()

    folder = ROOT / "dist" / args.name
    if not folder.is_dir():
        print(f"找不到目录 {folder}")
        print("先打包：双击 打包exe.bat，或执行 "
              "python -m PyInstaller --onedir --noconsole ... video_dedup.py")
        return 1

    ver = args.version or detect_version()
    out = ROOT / "dist" / f"{args.name}-{ver}-win64.zip"
    files = [p for p in folder.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    print(f"压缩 {folder.name}/ → {out.name}")
    print(f"  {len(files)} 个文件，原始 {total / 1048576:.1f} MB")

    t0 = time.time()
    # ZIP_DEFLATED 对 dll/pyd 压缩比一般，但能省下不少
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(files):
            z.write(p, arcname=str(Path(args.name) / p.relative_to(folder)))
        z.writestr(f"{args.name}/先读我.txt", READ_ME_FIRST.encode("utf-8"))
    print(f"  完成：{out}（{out.stat().st_size / 1048576:.1f} MB，"
          f"{time.time() - t0:.1f} 秒）")
    print(f"\n提示：用户解压后得到 {args.name}\\ 文件夹，双击里面的 "
          f"{args.name}.exe 即可运行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
