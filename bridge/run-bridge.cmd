@echo off
rem run-bridge.cmd - start the WeChat <-> DSH bridge (production config)
rem Must run on the same Windows desktop session as WeChat (not a service).
setlocal
cd /d "%~dp0"

rem Prefer the Python runtime that ships with DSH (numpy + Pillow are required).
set "PYTHON=python"
if not "%BRIDGE_PYTHON%"=="" set "PYTHON=%BRIDGE_PYTHON%"
if "%BRIDGE_PYTHON%"=="" (
  set "CAND1=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
  if exist "%CAND1%" set "PYTHON=%CAND1%"
)

set PYTHONIOENCODING=utf-8
chcp 65001 >nul 2>&1
echo [run-bridge] python=%PYTHON%
echo [run-bridge] cwd=%CD%
echo [run-bridge] logs=state\logs   shots=state\shots   health=state\health.json

"%PYTHON%" bridge.py %*
set "RC=%ERRORLEVEL%"
echo [run-bridge] exit=%RC%
if not "%1"=="" goto :eof
rem In production a crash should not close the console silently.
if not "%RC%"=="0" pause
exit /b %RC%
