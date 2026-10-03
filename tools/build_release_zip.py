# -*- coding: utf-8 -*-
"""构建发布用 zip：把项目文件打进 dist/videodedup-v1.1.zip（顶层带版本目录，解压即可用）"""
import hashlib
import os
import zipfile
from pathlib import Path

ROOT = Path(r"E:\pj\vchachong")
VERSION = "v1.1"
PKG_NAME = f"videodedup-{VERSION}"
OUT_DIR = ROOT / "dist"
OUT_ZIP = OUT_DIR / f"{PKG_NAME}.zip"

# 要打包的文件（相对 ROOT）——不含 .git / .workbuddy / 缓存 / dist 自身
FILES = [
    "video_dedup.py",
    "requirements.txt",
    "运行工具.bat",
    "安装依赖.bat",
    "README.md",
    "LICENSE",
    "docs/screenshot-cover.png",
    "docs/screenshot-duration-sort.png",
]

# 压缩包内附带一个"先读我"，降低上手门槛
READ_ME_FIRST = """怎么用（Windows）
==================================================
1. 解压到一个不带中文/空格的路径也行，中文路径同样支持
2. 双击「安装依赖.bat」  （只需一次，从 PyPI 下载 opencv-python 等库）
3. 双击「运行工具.bat」

需要 Python 3.8 以上，官方安装包默认自带 tkinter。
想先验证环境是否正常，可执行：
    python video_dedup.py --selftest

命令行批量模式：
    python video_dedup.py --scan "D:\\视频库" --csv 报告.csv

隐私说明
==================================================
本工具全程在本机运行，不联网、不上传任何视频或图片、不调用云端模型。
所有删除类操作都必须人工勾选并二次确认，默认走系统回收站。

详细说明见 README.md，界面截图见 docs/ 目录。
Copyright (c) 2026 seanfan   MIT License
"""


def main():
    OUT_DIR.mkdir(exist_ok=True)
    missing = [f for f in FILES if not (ROOT / f).exists()]
    if missing:
        raise SystemExit(f"缺少文件：{missing}")

    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in FILES:
            src = ROOT / rel
            # 统一用 / 作为分隔符，并加版本目录前缀
            arc = f"{PKG_NAME}/{rel.replace(os.sep, '/')}"
            zi = zipfile.ZipInfo.from_file(src, arc)
            zi.compress_type = zipfile.ZIP_DEFLATED
            # 固定时间戳，让重复打包结果可复现（便于校验）
            zi.date_time = (2026, 10, 4, 0, 0, 0)
            with open(src, "rb") as f:
                z.writestr(zi, f.read())
        # 额外附加一份"先读我"
        z.writestr(f"{PKG_NAME}/先读我.txt", READ_ME_FIRST.encode("utf-8"))

    size = OUT_ZIP.stat().st_size
    sha = hashlib.sha256(OUT_ZIP.read_bytes()).hexdigest()
    print(f"已生成：{OUT_ZIP}")
    print(f"大小：{size:,} 字节（{size/1024:.1f} KB）")
    print(f"SHA256：{sha}")
    print("\n压缩包内容：")
    with zipfile.ZipFile(OUT_ZIP) as z:
        for i in z.infolist():
            print(f"  {i.filename:<52} {i.file_size:>9,} 字节")
        bad = z.testzip()
        print("\n完整性校验：", "通过 ✓" if bad is None else f"损坏 ✗ ({bad})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
