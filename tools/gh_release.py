# -*- coding: utf-8 -*-
"""
把 dist/ 里的发布包创建/更新为 GitHub Release

说明：
  * Release 无法通过 git 推送完成，只能调 GitHub REST API；
  * 凭据从本地 Git 凭据管理器读取（git credential fill），
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
DEFAULT_VERSION = "v1.2.2"

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
    return f"""## 本地视频查重小工具 {version}（分发形式变更，功能零改动）

Windows 本地视频查重工具：递归扫描文件夹 → 完整列出所有视频 →
用**双通道指纹**（pHash + 灰度通道）自动找出同源视频并分组 →
人工勾选后移入回收站 / 移动到备份文件夹。

> **全程本地运行**：不联网、不上传任何视频或图片、不调用云端模型。
> **不需要安装 ffmpeg**：元信息来自 pymediainfo 自带的 MediaInfo，解码来自 OpenCV 内置的 FFmpeg。

> ## ⏹ 项目状态：已无限期停止开发
>
> 版本号继续维护，但**只改分发形式，不加功能**。本项目因没有新的具体需求，
> 自 2026-10-05 起无限期停止开发。
> 仓库保持公开可读，可自由 fork 与二次开发（MIT）。
> 功能边界与已知取舍见 `docs/PROJECT-STATUS.md`。

### 📦 下载

| 文件 | 大小 | 说明 |
|---|---|---|
{rows}

**普通用户直接下 `VideoDedupTool-{version}-win64.zip`** —— 不用装 Python、不用装任何依赖：

```
解压 → 双击 VideoDedupTool\\VideoDedupTool.exe → 选目录 → 开始扫描
```

> **这个 zip 和以前那个 zip 的区别**：以前发的是一个**单文件 exe**（`--onefile`，
> 每次双击都要先把约 100 MB 解压到临时目录，等 5~10 秒）；现在发的是
> **文件夹版**（`--onedir`，依赖就摆在文件夹里，启动瞬时）。
> 文件夹要整体使用、别把 exe 单独拖出来。exe 可以自由改名，比如改成「视频查重工具.exe」。

> **杀软提示**：本软件为自制工具，未做代码签名；所有代码都在本仓库里，可自行查阅核对。
> 若被杀软误报，把所在文件夹加进信任区即可。

### 🛠 本版变更（{version}）

**只改了「怎么发」，没改「怎么算」** —— 功能、参数、判定逻辑与 v1.2.1 完全一致，
源码改动仅一处版本号字符串。

- **分发形式：单文件 exe → onedir 文件夹 + zip。**
  原来的 `--onefile` 每次启动都要把约 100 MB 依赖解压到临时目录（双击后等 5~10 秒），
  而且这种"运行时自解压"的行为在个别杀软的启发式眼里与释放载荷的木马很像。
  改成文件夹版后**启动是瞬时的，也不再有自解压行为**，误报面小得多。
- **仓库不再分发单文件 exe**：历史 Release 里的 `VideoDedupTool.exe` 附件已移除，
  想自己出单文件版随时可以打（`打包exe.bat` 里有现成命令）。
- 打包脚本同步更新：`打包exe.bat` 改为 `--onedir` + 自动压 zip（新增 `tools/make_release_zip.py`）。
- **文案修正**：窗口标题与界面提示里的「本机」改为「本地」——
  介绍产品就说产品本身，不把开发机的说法带进来。

> 完整的功能与修复说明请看 [v1.2.1 的发布说明](https://github.com/{OWNER}/{REPO}/releases/tag/v1.2.1)
> （收尾版本，修复了项目审计发现的全部问题）。

### 🔒 校验（SHA256）

```
{sha_lines}
```

也可以直接看仓库 `dist/SHA256SUMS.txt`（由打包脚本生成）。

### 📄 许可

MIT License · Copyright (c) 2026 seanfan
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=DEFAULT_VERSION)
    ap.add_argument("--only-zip", action="store_true", help="只上传源码包，跳过免安装文件夹包")
    args = ap.parse_args()

    version = args.version
    pkg = f"videodedup-{version}"
    assets: list = []
    zip_path = ROOT / "dist" / f"{pkg}.zip"
    if not zip_path.exists():
        print(f"找不到源码包：{zip_path}\n请先执行 python tools/build_release_zip.py")
        return 1
    assets.append((zip_path, "源码 + 文档 + 启动脚本，解压即用"))
    # v1.2.2 起免安装版是 onedir 文件夹 + zip（不再发单文件 exe）
    exe_zip = ROOT / "dist" / f"VideoDedupTool-{version}-win64.zip"
    if exe_zip.exists() and not args.only_zip:
        assets.append((exe_zip, "**免安装版（文件夹）**：解压后双击里面的 exe，已内置全部依赖"))
    elif not args.only_zip:
        print(f"提示：没找到 {exe_zip}，将只发布源码包。")
        print("      先打包：双击 打包exe.bat（会同时生成文件夹版与 zip）")

    token = get_token()
    if not token:
        print("未从本地 Git 凭据管理器取到 GitHub 凭据，无法调用 API。")
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

    # 2) 清掉同名旧附件，避免重复；顺手清掉历史遗留的单文件 exe 附件
    status, old = api(f"/repos/{OWNER}/{REPO}/releases/{rid}/assets", token=token)
    existing = {a["name"]: a["id"] for a in old} if status == 200 else {}
    for path, _note in assets:
        if path.name in existing:
            print(f"  删除旧附件 {path.name} …")
            api(f"/repos/{OWNER}/{REPO}/releases/assets/{existing[path.name]}",
                "DELETE", token=token)
    for name, aid in existing.items():
        if name.lower().endswith(".exe"):
            print(f"  删除历史遗留的单文件 exe 附件 {name} …")
            api(f"/repos/{OWNER}/{REPO}/releases/assets/{aid}", "DELETE", token=token)

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
