@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8:replace
set PYTHONLEGACYWINDOWSSTDIO=utf-8
set PYTHONDONTWRITEBYTECODE=1
cd /d "%~dp0"

set "PYTHON_EXE="

REM 1) Prefer the bundled runtime plus project dependencies. This is the
REM environment that includes the optional local Whisper stack.
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
  if exist "%~dp0.runtime_deps\uvicorn" (
    set "PYTHONPATH=%~dp0.runtime_deps"
    set "PYTHON_EXE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    goto :check
  )
)

REM 2) Fall back to the local .venv if the bundled runtime is unavailable.
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -c "import fastapi, httpx, multipart, uvicorn, yt_dlp" >nul 2>&1
  if not errorlevel 1 (
    set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
    goto :check
  )
)

REM 3) Try system python (where python).
if not defined PYTHON_EXE (
  where python >nul 2>&1
  if not errorlevel 1 set "PYTHON_EXE=python"
)

:check
if not defined PYTHON_EXE (
  echo Python is not available. Run install_web.bat first.
  pause
  exit /b 1
)

"%PYTHON_EXE%" -c "import fastapi, httpx, multipart, uvicorn, yt_dlp"
if errorlevel 1 (
  echo.
  echo The selected Python runtime cannot load the Web dependencies.
  echo Runtime: %PYTHON_EXE%
  echo Run install_web.bat, then try again.
  pause
  exit /b 1
)

"%PYTHON_EXE%" -m web.launcher --project-root "%~dp0."
