@echo off
REM ===========================================================================
REM  IconSwap build script (PyInstaller)
REM  Output : dist\IconSwap.exe  (single file, no console window)
REM  Usage  : double-click this file, or run build_exe.bat in the project root
REM  Note   : messages are ASCII on purpose, so the script parses correctly
REM           under any Windows code page. Chinese docs: README-desktop.md
REM ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo [IconSwap] Checking PyInstaller ...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 goto :no_pyinstaller

echo [IconSwap] Checking pywebview ...
python -c "import webview" >nul 2>&1
if errorlevel 1 goto :no_pywebview

echo [IconSwap] Building ...
REM ---------------------------------------------------------------------------
REM  Onefile runtime temp dir (--runtime-tmpdir).
REM  Must be ASCII and creatable WITHOUT admin rights: the PyInstaller
REM  bootloader extracts here BEFORE any Python code runs, so the app itself
REM  cannot create it in time for the very first launch.
REM
REM  Candidate order (portability matters more than aesthetics):
REM    1. %PUBLIC%\IconSwapTemp      - parent always exists, writable by users,
REM                                    ASCII. Works on any machine, so the
REM                                    baked path is safe to ship.
REM    2. C:\IconSwapTemp            - only usable if it already exists
REM                                    (C:\ root needs admin to create), so a
REM                                    baked path here may not exist elsewhere.
REM    3. %LOCALAPPDATA%\IconSwapTemp- always writable, but the user name may
REM                                    contain non-ASCII characters.
REM  If none can be created the build falls back to default %TEMP%.
REM ---------------------------------------------------------------------------
set "ISW_TMPDIR="
call :try_tmpdir "%PUBLIC%\IconSwapTemp"
if not defined ISW_TMPDIR call :try_tmpdir "C:\IconSwapTemp"
if not defined ISW_TMPDIR call :try_tmpdir "%LOCALAPPDATA%\IconSwapTemp"

set "ISW_TMPDIR_OPT="
if defined ISW_TMPDIR (
    echo [IconSwap] Runtime temp dir: %ISW_TMPDIR%
    set "ISW_TMPDIR_OPT=--runtime-tmpdir "%ISW_TMPDIR%""
) else (
    echo [WARN] No writable ASCII temp dir found - building WITHOUT --runtime-tmpdir.
    echo        The app will use the default %%TEMP%%; if that path is unusable the
    echo        onefile bootloader reports "Could not create temporary directory!".
)

REM  SciPy/NumPy/Intel MKL, matplotlib, tcl/tk and Qt are not used by
REM  IconSwap; excluding them keeps the EXE small (MKL alone is ~400 MB).
REM  --distpath "." keeps the runnable exe at the project root (.\IconSwap.exe)
REM  instead of a nested dist\ folder. Do NOT use "%~dp0" here: it ends with a
REM  backslash, and "dir\" makes Windows command-line parsing treat the quote as
REM  escaped, swallowing every following argument.
REM  --paths "src" makes the bundler find the iconswap package + ui_window.
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name IconSwap ^
  --distpath "." ^
  --workpath "build" ^
  --icon "assets\iconswap.ico" ^
  --paths "src" ^
  %ISW_TMPDIR_OPT% ^
  --add-data "src\iconswap\templates;iconswap\templates" ^
  --add-data "src\iconswap\static;iconswap\static" ^
  --add-data "assets;assets" ^
  --add-data "bin;bin" ^
  --hidden-import webview ^
  --hidden-import webview.platforms.edgechromium ^
  --hidden-import webview.platforms.winforms ^
  --hidden-import clr ^
  --hidden-import pefile ^
  --hidden-import win32com ^
  --hidden-import win32com.client ^
  --hidden-import pythoncom ^
  --hidden-import pywintypes ^
  --hidden-import win32api ^
  --collect-all pythonnet ^
  --exclude-module numpy ^
  --exclude-module scipy ^
  --exclude-module pandas ^
  --exclude-module matplotlib ^
  --exclude-module tkinter ^
  --exclude-module PyQt5 ^
  --exclude-module PyQt6 ^
  --exclude-module PySide2 ^
  --exclude-module PySide6 ^
  --exclude-module qtpy ^
  --exclude-module IPython ^
  --exclude-module black ^
  --exclude-module yapf ^
  --exclude-module blib2to3 ^
  --exclude-module zmq ^
  --exclude-module pytest ^
  src\main.py

if errorlevel 1 goto :build_failed
if not exist "%~dp0IconSwap.exe" goto :missing_exe

echo.
echo [OK] Output: %~dp0IconSwap.exe
echo      Double-click it to open the IconSwap desktop window.
echo.
pause
endlocal
exit /b 0

:no_pyinstaller
echo.
echo [ERROR] PyInstaller is missing.
echo         Run: python -m pip install -r requirements-desktop.txt
pause
endlocal
exit /b 1

:no_pywebview
echo.
echo [ERROR] pywebview is missing.
echo         Run: python -m pip install -r requirements-desktop.txt
pause
endlocal
exit /b 1

:build_failed
echo.
echo [ERROR] PyInstaller failed. See the log above.
pause
endlocal
exit /b 1

:missing_exe
echo.
echo [ERROR] IconSwap.exe was not created in the project root.
pause
endlocal
exit /b 1

REM ---------------------------------------------------------------------------
REM  Subroutine: try to use / create a writable temp dir for --runtime-tmpdir.
REM  Sets ISW_TMPDIR when it works, leaves it empty otherwise.
REM ---------------------------------------------------------------------------
:try_tmpdir
if defined ISW_TMPDIR exit /b 0
if "%~1"=="" exit /b 0
if not exist "%~1" mkdir "%~1" 2>nul
if not exist "%~1" exit /b 0
REM  verify it is actually writable, not just present
echo probe>"%~1\.iconswap_probe" 2>nul
if not exist "%~1\.iconswap_probe" exit /b 0
del "%~1\.iconswap_probe" >nul 2>&1
set "ISW_TMPDIR=%~1"
exit /b 0
