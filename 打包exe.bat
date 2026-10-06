@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   打包免安装版（PyInstaller）
echo ============================================================
echo.

set "PYCMD=python"
py -3 --version >nul 2>nul && set "PYCMD=py -3"
%PYCMD% --version >nul 2>nul
if errorlevel 1 goto NOPY

echo [1/4] 检查运行依赖...
%PYCMD% -c "import cv2,numpy,PIL,imagehash,pymediainfo" >nul 2>nul
if errorlevel 1 (
  echo       缺少依赖，正在安装...
  %PYCMD% -m pip install -r requirements.txt
  if errorlevel 1 goto FAIL
)

echo [2/4] 检查 PyInstaller...
%PYCMD% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo       未安装，正在安装 PyInstaller...
  %PYCMD% -m pip install pyinstaller
  if errorlevel 1 goto FAIL
)

echo [3/4] 打包「文件夹版」到 dist\VideoDedupTool\（推荐），约 1-2 分钟...
%PYCMD% -m PyInstaller --onedir --noconsole --noconfirm --clean ^
  --name VideoDedupTool ^
  --distpath dist --workpath build --specpath build video_dedup.py
if errorlevel 1 goto FAIL

echo [4/4] 打成发布用 zip ...
%PYCMD% tools\make_release_zip.py
if errorlevel 1 goto FAIL

echo.
echo ============================================================
echo   打包完成，dist\ 里有两样东西：
echo     dist\VideoDedupTool\                    文件夹版（推荐）
echo     dist\VideoDedupTool-v1.2.2-win64.zip    上面这个的压缩包
echo.
echo   为什么默认发文件夹版而不是单文件 exe：
echo     1) 单文件每次启动都要把 ~100MB 依赖解压到临时目录，双击后要等 5~10 秒；
echo        文件夹版是解压好的，启动是瞬时的。
echo     2) 单文件的自解压行为容易被杀软启发式误判（本工具就是因此换成文件夹版的）。
echo.
echo   想要单文件 exe 的话，再跑一次：
echo     %PYCMD% -m PyInstaller --onefile --noconsole --noconfirm --clean ^
echo       --name VideoDedupTool-onefile --distpath dist --workpath build ^
echo       --specpath build video_dedup.py
echo.
echo   其它说明：
echo     - exe / 文件夹都可以自由改名，比如改成「视频查重工具」
echo     - 本工具不需要 ffmpeg（元信息用 pymediainfo 自带的 MediaInfo，
echo       解码用 OpenCV 内置的 FFmpeg）
echo     - 无控制台打包，双击运行时看不到命令行输出是正常的；
echo       想看 --selftest / --scan 的输出，在 cmd 里 cd 到该目录再执行 exe 即可
echo       （带参数运行时会自动附着父控制台）
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
