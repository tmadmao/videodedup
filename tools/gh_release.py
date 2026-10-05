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
DEFAULT_VERSION = "v1.2.1"

CONTENT_TYPES = {
    ".zip": "application/zip",
    ".exe": "application/vnd.microsoft.portable-executable",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


def get_token() -> str:
    """
    从 Git 凭据管理器取 GitHub 凭据（不打印、不落盘）。

    注意：凭据管理器首次调用可能要 5~10 秒（尤其刚连上代理时），超时给宽一点，
    否则会误判成"没有凭据"。
    """
    try:
        p = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, cwd=str(ROOT), timeout=180,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except Exception as e:
        print(f"读取 Git 凭据失败：{e}")
        print("  提示：可先手动执行  git credential fill  确认凭据可用（应输出 password=...）")
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
    return f"""## 本地视频查重小工具 {version}（收尾版本）

Windows 本地视频查重工具：递归扫描文件夹 → 完整列出所有视频 →
用**双通道指纹**（pHash + 灰度通道）自动找出同源视频并分组 →
人工勾选后移入回收站 / 移动到备份文件夹。

> **全程本地运行**：不联网、不上传任何视频或图片、不调用云端模型。
> **不需要安装 ffmpeg**：元信息来自 pymediainfo 自带的 MediaInfo，解码来自 OpenCV 内置的 FFmpeg。

> ## ⏹ 项目状态：已无限期停止开发
>
> v1.2.1 是**收尾版本**，只修缺陷、不加功能。本项目因没有新的具体需求，
> 自 2026-10-05 起无限期停止开发，不再有新版本。
> 仓库保持公开可读，可自由 fork 与二次开发（MIT）。
> 功能边界与已知取舍见 `docs/PROJECT-STATUS.md`。

### 📦 下载

| 文件 | 大小 | 说明 |
|---|---|---|
{rows}

**普通用户直接下 exe 就行** —— 不用装 Python、不用装任何依赖，双击打开。
（首次启动要把约 100 MB 解压到临时目录，等几秒属正常；exe 可自由改名。）

### 🛠 本版修复（{version}）

本次发布的内容**全部是修复**，问题来自三版开发后的项目审计（`docs/AUDIT-2026-10-05.md`）。

- **修：命令行结果不可复现。** 同一目录、同一参数连跑多次，"建议保留"会在两个**字节完全相同**
  的副本之间来回跳（审计实测：同一条命令 3 次跑出 2 种结果）。
  根因是命令行用 `as_completed` 收集结果、候选顺序随线程完成次序变化，
  而质量判据最后一级在数值相等时取"先遇到的"。现已固定按路径排序 + 确定化平局裁决：
  **同输入必得同输出，命令行与界面也指向同一个文件**。
- **修：「重新分组」忽略灰度通道设置。** 「灰度通道阈值」滑杆与「忽略黑/白像素」开关
  此前只在"开始扫描"时生效，点"重新分组"（以及移入回收站/备份后的自动重分组）
  会被静默忽略。现已一并传入；勾选框改动后立即重新分组。
- **修：自检对抗水印的结论过于乐观。** 原自检只用"不透明纯白块"水印，得出
  "掩码把相似度从 95% 拉回 99.96%"；现新增**半透明台标**用例（贴近真实平台水印），
  实测该场景掩码增益仅 +0.5pp —— 半透明水印合成后既不到近白也不到近黑，
  **掩码排除不到它，真正把它认出来的是 32×32 灰度通道本身**。文档口径已修正。
- **修：18 处对话框未设置父窗口**，少数窗口环境下消息框会落到主窗口之后，
  界面看起来像死机。
- **修：4 个参数"存了不读"**（时长容差 / 采样帧数 / 跳过小于(MB) / 上次备份目录
  每次启动都被重置为默认值）。
- 另修：缩略图缓存版本号未跟随主缓存升级、一处重复的异常分支，以及 README 中
  下载包版本号、自检项数、重复表格行等文档不一致。

### 📊 实测识别效果

同一段视频的四种变形 + 一个无关视频，两个通道各自的相似度：

| 变形 | pHash | 灰度通道 |
| --- | --- | --- |
| 复制改名 | 100.0% | 100.0% |
| 二次转码（854×480 CRF32） | 98.4% | 99.6% |
| 轻微裁切 | 92.5% | 97.7% |
| 加水印 | **86.6%** | **99.0%** |
| **完全无关的视频** | 52.1% | 74.9% |

默认 `pHash 0.80` + `灰度通道 0.92`，任一命中即判重。

> **关于「掩码」的适用范围（如实说明）**：抗水印的主力是**灰度通道本身**，
> 不是掩码。掩码（忽略近黑 ≤32 / 近白 ≥240）真正有用的是黑边、纯白台标、
> 烧制白字幕；对最常见的**半透明水印**几乎没有增益（合成后这些像素既不到 240
> 也不低于 32，掩码排除不到它们），并且掩码会抬高异源相似度
> （实测 66.1% → 74.9%），略微压缩安全区。`--selftest` 现在两种水印都会测。

### 🔒 校验（SHA256）

```
{sha_lines}
```

也可以直接看仓库 `dist/SHA256SUMS.txt`（v1.2.1 起由打包脚本生成）。

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
