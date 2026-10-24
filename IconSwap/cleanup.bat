@echo off
rem ===========================================================================
rem  IconSwap cleanup launcher (ASCII only, so it parses on any code page)
rem    double-click          -> clean
rem    cleanup.bat -n        -> dry-run
rem    cleanup.bat --exe     -> also delete IconSwap.exe (asks first)
rem  All real logic lives in cleanup.py.
rem ===========================================================================
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python 3.10+ not found in PATH.
  echo         Install Python, or run: py -3 cleanup.py
  pause
  exit /b 1
)

python "%~dp0cleanup.py" %*
set "CODE=%ERRORLEVEL%"
echo.
pause
exit /b %CODE%
