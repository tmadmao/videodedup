# -*- coding: utf-8 -*-
"""
把 dist/ 里的发布包创建/更新为 GitHub Release

说明：
  * Release 无法通过 git 推送完成，只能调 GitHub REST API；
  * 凭据从本机 Git 凭据管理器读取（git credential fill），
    **只在内存中使用，不写入磁盘、不打印到输出**；
  * 已存在同名 release / asset 时会先更新说明、删掉同名附件再上传，可重复执行。

用法：
    python tools/gh_release.py                    :: 默认版本
    python tools/gh_release.py --version v1.3
    python tools/gh_release.py --only-zip         :: 只发源码包（跳过 100MB 的 exe）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER, REPO = "tmadmao", "videodedup"
API = "https://api.github.com"
DEFAULT_VERSION = "v1.2"

CONTENT_TYPES = {
    ".zip": "application/zip",
    ".exe": "application/vnd.microsoft.portable-executable",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


def get_token() -> str:
    """从 Git 凭据管理器取 GitHub 凭据（不打印、不落盘）"""
    try:
        p = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, cwd=str(ROOT), timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except Exception as e:
        print(f"读取 Git 凭据失败：{e}")
        return ""
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def api(path_or_url: str, method: str = "GET", data: bytes | None = None,
        token: str = "", ctype: str = "application/json", timeout: int = 180):
    url = path_or_url if path_or_url.startswith("http") else API + path_or_url
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "videodedup-release")
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            body = json.loads(body).get("message", body)
        except Exception:
            pass
        return e.code, {"message": body}
    except Exception as e:
        return 0, {"message": f"{type(e).__name__}: {e}"}


def build_body(version: str, assets: list) -> str:
    rows = "\n".join(
        f"| `{p.name}` | {p.stat().st_size:,} 字节 | {note} |" for p, note in assets
    )
    sha_lines = "\n".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p, _ in assets
    )
    return f"""## 本地视频查重小工具 {version}

Windows 本地视频查重工具：递归扫描文件夹 → 完整列出所有视频 →
用**双通道指纹**（pHash + 灰度掩码）自动找出同源视频并分组 →
人工勾选后移入回收站 / 移动到备份文件夹。

> **全程本地运行**：不联网、不上传任何视频或图片、不调用云端模型。
> **不需要安装 ffmpeg**：元信息来自 pymediainfo 自带的 MediaInfo，解码来自 OpenCV 内置的 FFmpeg。

### 📦 下载

| 文件 | 大小 | 说明 |
|---|---|---|
{rows}

**普通用户直接下 exe 就行** —— 不用装 Python、不用装任何依赖，双击打开。
（首次启动要把约 100 MB 解压到临时目录，等几秒属正常；exe 可自由改名。）

### ✨ 本版新增（{version}）

- **抗水印的「灰度掩码通道」**：不再靠"放宽阈值"认水印，而是把**近黑(≤32)/近白(≥240)像素排除在计分之外**。
  实测：阈值收紧到 0.92（严到平均汉明距离 ≤5.1/64）时，带水印副本的 pHash 只有 86.6% 已不达标，
  **仍被掩码通道捞回**；而完全无关的视频不会被误判。
- **组代表互验，防止"连环并组"**：A~B、B~C 不再必然把 A、C 连成一组
  （此前在"同系列不同集"的目录里会把整个目录并成一个巨大的假组）。
- **「建议保留」改为逐级淘汰 + 近似平局容差**：判据 `时长→分辨率→每像素位数→码率→帧率→大小`，
  并显示理由（如"建议保留：xxx.mp4（时长更长）"）。旧版会让"200 kbps 的 1080p"压过"8 Mbps 的 720p"。
- 修复：编码名显示成数字（FLV 的 H.264 曾显示为「7」）；中文 Windows 控制台下打包版会崩溃（GBK 编不出 `✓`）。
- 抽帧自适应：短视频用顺序 grab、长视频用逐位置 seek（30 秒视频快 2.1×、5 分钟视频快 4.2×），
  两条路取到的帧**完全一致**。

### 📊 实测识别效果

同一段视频的四种变形，两个通道各自的相似度：

| 变形 | pHash | 灰度掩码 |
| --- | --- | --- |
| 复制改名 | 100.0% | 100.0% |
| 二次转码（854×480 CRF32） | 98.4% | 99.6% |
| 轻微裁切 | 92.5% | 97.7% |
| 加水印 | **86.6%** | **99.0%** |
| **完全无关的视频** | 52.1% | 74.9% |

默认 `pHash 0.80` + `灰度掩码 0.92`，任一命中即判重。

### 🔒 校验（SHA256）

```
{sha_lines}
```

### 📄 许可

MIT License · Copyright (c) 2026 seanfan
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=DEFAULT_VERSION)
    ap.add_argument("--only-zip", action="store_true", help="只上传源码包，跳过 exe")
    args = ap.parse_args()

    version = args.version
    pkg = f"videodedup-{version}"
    assets: list = []
    zip_path = ROOT / "dist" / f"{pkg}.zip"
    if not zip_path.exists():
        print(f"找不到源码包：{zip_path}\n请先执行 python tools/build_release_zip.py")
        return 1
    assets.append((zip_path, "源码 + 文档 + 启动脚本，解压即用"))
    exe_path = ROOT / "dist" / "VideoDedupTool.exe"
    if exe_path.exists() and not args.only_zip:
        assets.append((exe_path, "**免安装版：双击即用**，已内置全部依赖"))
    elif not args.only_zip:
        print(f"提示：没找到 {exe_path}，将只发布源码包。")
        print("      先打包：双击 打包exe.bat，或执行 tools/gh_release.py 前先跑 PyInstaller")

    token = get_token()
    if not token:
        print("未从本机 Git 凭据管理器取到 GitHub 凭据，无法调用 API。")
        print(f"可改为手动发布：在仓库 Releases 页面选择 tag {version}，把 dist/ 里的文件拖进去。")
        return 2

    body = build_body(version, assets)
    release_name = f"{version} — 本地视频查重小工具"

    # 1) 已存在同 tag 的 release 就复用并更新，否则新建
    status, rel = api(f"/repos/{OWNER}/{REPO}/releases/tags/{version}", token=token)
    if status == 200:
        print(f"已存在 release {version}，更新说明…")
        status, rel = api(f"/repos/{OWNER}/{REPO}/releases/{rel['id']}", "PATCH",
                          json.dumps({"name": release_name, "body": body}).encode(), token)
    else:
        print(f"创建 release {version} …")
        status, rel = api(f"/repos/{OWNER}/{REPO}/releases", "POST",
                          json.dumps({"tag_name": version, "target_commitish": "main",
                                      "name": release_name, "body": body,
                                      "draft": False, "prerelease": False}).encode(), token)
    if status not in (200, 201):
        print(f"创建/更新 release 失败（HTTP {status}）：{rel}")
        return 3
    rid = rel["id"]
    print(f"  成功：{rel['html_url']}")

    # 2) 清掉同名旧附件，避免重复
    status, old = api(f"/repos/{OWNER}/{REPO}/releases/{rid}/assets", token=token)
    existing = {a["name"]: a["id"] for a in old} if status == 200 else {}
    for path, _note in assets:
        if path.name in existing:
            print(f"  删除旧附件 {path.name} …")
            api(f"/repos/{OWNER}/{REPO}/releases/assets/{existing[path.name]}",
                "DELETE", token=token)

    # 3) 上传附件（大文件给更长的超时）
    upload_base = rel["upload_url"].split("{")[0]
    for path, _note in assets:
        size = path.stat().st_size
        ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        print(f"上传 {path.name}（{size:,} 字节，{size / 1048576:.1f} MB）…")
        upload_url = upload_base + f"?name={path.name}"
        timeout = 900 if size > 20 * 1048576 else 300
        status, asset = api(upload_url, "POST", path.read_bytes(), token, ctype,
                            timeout=timeout)
        if status not in (200, 201):
            print(f"  上传失败（HTTP {status}）：{asset}")
            return 4
        print(f"  成功：{asset['browser_download_url']}")

    print("\n发布完成 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
