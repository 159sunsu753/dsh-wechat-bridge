@echo off
rem start-bridge.cmd - start the watchdog hidden; it starts/restarts bridge.py.
setlocal
cd /d "%~dp0"
wscript.exe //nologo "%~dp0start-watchdog.vbs"
exit /b 0
