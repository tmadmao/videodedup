@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================================
echo   本地视频查重小工具 - 依赖安装
echo   （仅从 PyPI 下载 Python 库，不涉及视频上传）
echo ================================================
echo.
py -3 --version >nul 2>nul
if %errorlevel%==0 (
    py -3 -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
) else (
    python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
)
echo.
echo 安装结束。可执行「运行工具.bat」启动程序，或运行自检：
echo     python video_dedup.py --selftest
pause
