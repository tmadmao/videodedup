@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动「本地视频查重小工具」（全程本地运行，不联网上传）...
py -3 --version >nul 2>nul
if %errorlevel%==0 (
    py -3 video_dedup.py
) else (
    python video_dedup.py
)
if %errorlevel% neq 0 (
    echo.
    echo 启动失败。请先双击运行「安装依赖.bat」，或检查是否已安装 Python 3.8+。
)
pause
