@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo 请先运行 install_web.bat。
  pause
  exit /b 1
)

where uv >nul 2>&1
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install -r whisper_requirements.txt
) else (
  uv pip install --python ".venv\Scripts\python.exe" -r whisper_requirements.txt
)
if errorlevel 1 goto FAILED

echo.
echo Whisper 安装完成。重新启动字幕工作台后生效。
pause
exit /b 0

:FAILED
echo.
echo Whisper 安装失败。请检查网络、Python 版本和 CUDA 环境。
pause
exit /b 1
