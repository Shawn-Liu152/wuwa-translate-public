@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "SUBTITLE_PROJECT_MARKER=%~dp0."
echo Stopping this subtitle console...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$marker = $env:SUBTITLE_PROJECT_MARKER; $matches = @(Get-CimInstance Win32_Process | Where-Object { $command = [string]$_.CommandLine; $_.Name -in @('python.exe', 'pythonw.exe') -and $command.IndexOf('-m web.launcher', [StringComparison]::OrdinalIgnoreCase) -ge 0 -and $command.IndexOf($marker, [StringComparison]::OrdinalIgnoreCase) -ge 0 }); if (-not $matches) { Write-Host 'No subtitle console started from this folder was found.'; exit 3 }; foreach ($process in $matches) { Stop-Process -Id $process.ProcessId -ErrorAction Stop; Write-Host ('Stopped PID ' + $process.ProcessId) }"

if errorlevel 1 (
  echo Nothing was stopped. Close the start_web.bat window or press Ctrl+C there.
) else (
  echo Server stopped.
)
pause
