@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   打包免安装版 exe（PyInstaller）
echo ============================================================
echo.

set "PYCMD=python"
py -3 --version >nul 2>nul && set "PYCMD=py -3"
%PYCMD% --version >nul 2>nul
if errorlevel 1 goto NOPY

echo [1/3] 检查运行依赖...
%PYCMD% -c "import cv2,numpy,PIL,imagehash,pymediainfo" >nul 2>nul
if errorlevel 1 (
  echo       缺少依赖，正在安装...
  %PYCMD% -m pip install -r requirements.txt
  if errorlevel 1 goto FAIL
)

echo [2/3] 检查 PyInstaller...
%PYCMD% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo       未安装，正在安装 PyInstaller...
  %PYCMD% -m pip install pyinstaller
  if errorlevel 1 goto FAIL
)

echo [3/3] 开始打包，首次约 1-2 分钟，请稍候...
echo.
%PYCMD% -m PyInstaller --onefile --noconfirm --clean --name VideoDedupTool --distpath dist --workpath build --specpath build video_dedup.py
if errorlevel 1 goto FAIL

echo.
echo ============================================================
echo   打包完成
echo   产物：dist\VideoDedupTool.exe   约 100 MB
echo.
echo   说明：
echo     - 首次启动要把约 100MB 解压到临时目录，5-10 秒属正常
echo     - exe 可自由改名，比如改成「视频查重工具.exe」
echo     - 若启动太慢或被杀软拦截，改用文件夹模式：
echo       把本脚本里的 --onefile 删掉，再运行一次
echo ============================================================
pause
exit /b 0

:NOPY
echo [错误] 没检测到 Python，请先双击「安装依赖.bat」
pause
exit /b 1

:FAIL
echo.
echo [失败] 请把上面的报错信息发出来
pause
exit /b 1
