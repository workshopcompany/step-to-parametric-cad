@echo off
rem ===========================================================================
rem  step2param - web UI (Windows)
rem
rem  Starts the browser interface on http://127.0.0.1:8092
rem    - pick a STEP file from the step/ folder, or upload one
rem    - see it in 3D right away (drag to rotate)
rem    - run the parametric conversion and watch the log
rem    - compare the original and the built model in 3D, including a
rem      red/green diff view that shows exactly where they differ
rem
rem  Run setup.bat first (once). This file only starts the server.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title step2param - web UI

chcp 65001 >nul 2>nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" (
    echo [ERROR] step2param is not installed yet.
    echo         Run setup.bat first, then run_webui.bat again.
    pause
    exit /b 1
)

if not exist "step" mkdir step
if not exist "out" mkdir out

echo ============================================================
echo  step2param - web UI
echo.
echo  Opening http://127.0.0.1:8092 in your browser.
echo  Keep this window open while you use the web UI.
echo  Press Ctrl+C here to stop the server.
echo ============================================================
echo.
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:8092"
"%VPY%" -m step2param.webui
set "RC=%ERRORLEVEL%"
echo.
echo Server stopped (exit code %RC%).
pause
exit /b %RC%
