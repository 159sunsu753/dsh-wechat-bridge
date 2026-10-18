@echo off
rem stop-bridge.cmd - stop watchdog + bridge.py (does not touch WeChat/DSH).
setlocal
cd /d "%~dp0"
set "PIDFILE=%~dp0state\watchdog.pid"
if not exist "%PIDFILE%" goto :killbridge
for /f "usebackq delims=" %%p in ("%PIDFILE%") do (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Stop-Process -Id %%p -Force -ErrorAction SilentlyContinue" >nul 2>&1
)
del /q "%PIDFILE%" >nul 2>&1
goto :killbridge
:killbridge
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*bridge.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
echo [stop-bridge] stopped (watchdog + bridge.py)
exit /b 0
