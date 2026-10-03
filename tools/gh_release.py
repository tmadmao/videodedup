# -*- coding: utf-8 -*-
"""
把 dist/ 里的发布包创建/更新为 GitHub Release（v1.1）

说明：
  * Release 无法通过 git 推送完成，只能调 GitHub REST API；
  * 凭据从本机 Git 凭据管理器读取（git credential fill），
    **只在内存中使用，不写入磁盘、不打印到输出**；
  * 已存在同名 release / asset 时会先清理再上传，可重复执行。

用法：python tools/gh_release.py
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER, REPO = "tmadmao", "videodedup"
TAG = "v1.1"
RELEASE_NAME = "v1.1 — 本地视频查重小工具"
ZIP = ROOT / "dist" / "videodedup-v1.1.zip"
API = "https://api.github.com"


def get_token() -> str:
    """从 Git 凭据管理器取 GitHub 凭据（不打印、不落盘）"""
    try:
        p = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, cwd=str(ROOT), timeout=60,
            env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except Exception as e:
        print(f"读取 Git 凭据失败：{e}")
        return ""
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def api(path_or_url: str, method: str = "GET", data: bytes | None = None,
        token: str = "", ctype: str = "application/json"):
    url = path_or_url if path_or_url.startswith("http") else API + path_or_url
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "videodedup-release")
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read()
            return r.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            body = json.loads(body).get("message", body)
        except Exception:
            pass
        return e.code, {"message": body}


def build_body(sha: str, size: int) -> str:
    return f"""## 本地视频查重小工具 v1.1

Windows 本地视频查重工具：递归扫描文件夹 → 完整列出所有视频 → 用**视频帧感知哈希（pHash）**
自动找出同源视频并分组 → 人工勾选后移入回收站 / 移动到备份文件夹。

> **全程本地运行**：不联网、不上传任何视频或图片、不调用云端模型。

### 📦 下载

**`videodedup-v1.1.zip`**（{size:,} 字节）—— 解压即用，含源码、依赖清单、启动脚本、说明文档与界面截图

### 🚀 使用（Windows）

1. 解压到任意目录
2. 双击 **`安装依赖.bat`**（只需一次，从 PyPI 下载 opencv-python 等库）
3. 双击 **`运行工具.bat`**

环境要求：Python 3.8 以上（官方安装包默认自带 tkinter）。
想先验证环境：`python video_dedup.py --selftest`

### ✨ 本版功能

- 递归扫描 + **完整列出全部视频**（缩略图 / 路径 / 大小 / 分辨率 / 时长 / 码率 / 编码）
- pHash 帧哈希识别同源视频：**复制改名 / 二次转码 / 加水印 / 轻微裁切** 均可识别
- 相似度阈值、时长容差可调；相似视频自动分组，并标注「建议保留」项
- 缩略图来源可切换：**封面（首帧，自动避开黑场）** / 内容帧 / 中间帧
- 所有列**可点击排序**（时长/大小/分辨率/码率…）+ 平铺视图，便于按时长定位可疑项
- 移入回收站 / 移动到备份文件夹；**绝不自动删除**，必须人工勾选 + 二次确认

### 📊 实测识别效果

同一段视频的四种变形，逐帧 pHash 平均汉明距离（0~64，越小越像）：

| 变形 | 平均距离 | 相似度 |
| --- | --- | --- |
| 复制改名 | 0.0 | 100% |
| 二次转码（854×480 CRF32） | 1.2 | 98% |
| 轻微裁切 | 4.8 | 93% |
| 加水印 | 8.6 | 87% |
| **完全无关的视频** | **31.6** | 51% |

同源最差 8.6、异源 31.6，中间有很宽的安全区，因此默认阈值取 **0.80**（≈ 平均距离 ≤ 12.8/64）。

### 🔒 校验

```
SHA256: {sha}
```

### 📄 许可

MIT License · Copyright (c) 2026 seanfan
"""


def main() -> int:
    if not ZIP.exists():
        print(f"找不到发布包：{ZIP}\n请先执行 python tools/build_release_zip.py")
        return 1
    token = get_token()
    if not token:
        print("未从本机 Git 凭据管理器取到 GitHub 凭据，无法调用 API。")
        print("可改为手动发布：在仓库 Releases 页面选择 tag v1.1，把 dist/ 里的 zip 拖进去即可。")
        return 2

    sha = hashlib.sha256(ZIP.read_bytes()).hexdigest()
    size = ZIP.stat().st_size
    body = build_body(sha, size)

    # 1) 已存在同 tag 的 release 就复用并更新，否则新建
    status, rel = api(f"/repos/{OWNER}/{REPO}/releases/tags/{TAG}", token=token)
    if status == 200:
        print(f"已存在 release {TAG}，更新说明…")
        status, rel = api(f"/repos/{OWNER}/{REPO}/releases/{rel['id']}", "PATCH",
                          json.dumps({"name": RELEASE_NAME, "body": body}).encode(), token)
    else:
        print(f"创建 release {TAG} …")
        status, rel = api(f"/repos/{OWNER}/{REPO}/releases", "POST",
                          json.dumps({"tag_name": TAG, "target_commitish": "main",
                                      "name": RELEASE_NAME, "body": body,
                                      "draft": False, "prerelease": False}).encode(), token)
    if status not in (200, 201):
        print(f"创建/更新 release 失败（HTTP {status}）：{rel}")
        return 3
    rid = rel["id"]
    print(f"  成功：{rel['html_url']}")

    # 2) 清掉同名旧附件，避免重复
    status, assets = api(f"/repos/{OWNER}/{REPO}/releases/{rid}/assets", token=token)
    if status == 200:
        for a in assets:
            if a["name"] == ZIP.name:
                print(f"  删除旧附件 {a['name']} …")
                api(f"/repos/{OWNER}/{REPO}/releases/assets/{a['id']}", "DELETE", token=token)

    # 3) 上传附件（走 uploads.github.com）
    print(f"上传附件 {ZIP.name}（{size:,} 字节）…")
    upload_url = rel["upload_url"].split("{")[0] + f"?name={ZIP.name}"
    status, asset = api(upload_url, "POST", ZIP.read_bytes(), token, "application/zip")
    if status not in (200, 201):
        print(f"上传失败（HTTP {status}）：{asset}")
        return 4
    print(f"  成功：{asset['browser_download_url']}")
    print(f"  下载次数：{asset.get('download_count', 0)}")
    print("\n发布完成 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
