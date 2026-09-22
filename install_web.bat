@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where uv >nul 2>&1
if not errorlevel 1 goto INSTALL_WITH_UV

where python >nul 2>&1
if errorlevel 1 (
  echo Python and uv are both unavailable.
  echo Install uv from https://docs.astral.sh/uv/ and run this file again.
  pause
  exit /b 1
)

python -m venv .venv
if errorlevel 1 goto FAILED
".venv\Scripts\python.exe" -m pip install --require-hashes --only-binary=:all: -r web_requirements.txt
if errorlevel 1 goto FAILED
goto INSTALLED

:INSTALL_WITH_UV
uv venv .venv --python 3.11
if errorlevel 1 goto FAILED
uv pip install --python ".venv\Scripts\python.exe" --require-hashes --only-binary=:all: -r web_requirements.txt
if errorlevel 1 goto FAILED

:INSTALLED

echo.
echo Installation complete. Double-click start_web.bat to open the subtitle console.
pause
exit /b 0

:FAILED
echo.
echo Installation failed. Check Python and your network connection.
pause
exit /b 1
