@echo off
rem ===================================================================
rem  AI Radio - launcher
rem
rem  IMPORTANT: keep this file ASCII-only, with CRLF line endings.
rem  cmd.exe reads .bat files using the console codepage, so Chinese
rem  text combined with "chcp 65001" corrupts its parser (and LF-only
rem  line endings break it too).  All Chinese messages are printed by
rem  Python instead, which handles UTF-8 properly.
rem ===================================================================

cd /d "%~dp0"

set "PYEXE="

rem 1) the official Python launcher
where py >nul 2>nul
if not errorlevel 1 set "PYEXE=py"

rem 2) plain python on PATH
if not defined PYEXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)

rem 3) make sure it really runs.  The Microsoft Store ships a stub named
rem    python.exe that just opens the Store page instead of running Python.
if defined PYEXE (
    "%PYEXE%" -c "import sys" >nul 2>nul
    if errorlevel 1 set "PYEXE="
)

if not defined PYEXE goto nopython

"%PYEXE%" app.py
if errorlevel 1 goto failed
exit /b 0

:nopython
echo.
echo   [ERROR] Python not found, or it is not working.
echo.
echo   Please install Python 3.10 or newer:
echo       https://www.python.org/downloads/
echo.
echo   During installation, remember to tick:
echo       "Add python.exe to PATH"
echo.
pause
exit /b 1

:failed
echo.
echo   The program exited with an error.
echo   Please send a screenshot of the messages above.
echo.
pause
exit /b 1