@echo off
rem ===========================================================================
rem  step2param - run (Windows)
rem
rem  Double-click  -> asks for the STEP file and the output folder.
rem  Or use it from a terminal:
rem      run.bat examples\output_flange.step
rem      run.bat examples\output_flange.step --iso --ct
rem      run.bat --step part.step --out out\part --iso --refine-iter 3
rem
rem  The FreeCAD console path saved by setup.bat is added automatically.
rem ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title step2param

rem The program prints Korean status messages; without UTF-8 the Windows
rem console code page makes it die with UnicodeEncodeError (exit code 1)
rem even when every check passed. chcp fixes the display, PYTHONIOENCODING
rem fixes redirected output as well.
chcp 65001 >nul 2>nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" (
    echo [ERROR] step2param is not installed yet.
    echo         Run setup.bat first, then run.bat again.
    pause
    exit /b 1
)

rem ---- freecadcmd path remembered by setup.bat
set "FREECADCMD="
if exist ".venv\freecadcmd.txt" (
    for /f "usebackq delims=" %%P in (".venv\freecadcmd.txt") do set "FREECADCMD=%%P"
)

rem ---- dispatch: no arguments -> interactive, .step/.stp -> single part,
rem      anything else -> passed straight through to the program
if "%~1"=="" goto :interactive
if /i "%~x1"==".step" goto :one_step
if /i "%~x1"==".stp" goto :one_step
goto :passthrough

:one_step
set "STEPFILE=%~1"
if not exist "!STEPFILE!" (
    echo [ERROR] STEP file not found: !STEPFILE!
    pause
    exit /b 1
)
for %%F in ("!STEPFILE!") do set "PART=%%~nF"
set "OUTDIR=out\!PART!"
set "EXTRA="
shift
:extra_loop
if "%~1"=="" goto :extra_done
set "EXTRA=!EXTRA! "%~1""
shift
goto :extra_loop
:extra_done
set "NEED_FC=1"
set "ARGS=--step "!STEPFILE!" --out "!OUTDIR!"!EXTRA!"
goto :add_freecad

:passthrough
set "ARGS=%*"
goto :add_freecad

:interactive
echo ============================================================
echo  step2param
echo  STEP (+ optional 2D drawing)  -^>  parametric FreeCAD model
echo ============================================================
echo.
echo Drag and drop your .step / .stp file into this window, then
set /p "STEPFILE=press Enter: "
if not defined STEPFILE (
    echo [ERROR] No STEP file was given.
    pause
    exit /b 1
)
set "STEPFILE=%STEPFILE:"=%"
if not exist "!STEPFILE!" (
    echo [ERROR] File not found: !STEPFILE!
    pause
    exit /b 1
)
for %%F in ("!STEPFILE!") do set "PART=%%~nF"
set "DEFOUT=out\!PART!"
echo.
set /p "OUTDIR=Output folder [Enter = !DEFOUT!]: "
if not defined OUTDIR set "OUTDIR=!DEFOUT!"
set "OUTDIR=%OUTDIR:"=%"

echo.
echo Optional: the 2D drawing data (HLR .json) for the cross-check.
set /p "HLR=Drawing json [Enter = skip]: "
if defined HLR set "HLR=%HLR:"=%"
if defined HLR if not exist "!HLR!" (
    echo   not found - cross-check skipped.
    set "HLR="
)

echo.
set "OPT="
set /p "ISOYN=Write isometric views + montage (--iso)? [Y/n]: "
if /i not "!ISOYN!"=="n" set "OPT=!OPT! --iso"
set /p "REFN=Automatic refinement iterations [3]: "
if not defined REFN set "REFN=3"

set "NEED_FC=1"
set "ARGS=--step "!STEPFILE!" --out "!OUTDIR!"!OPT! --refine-iter !REFN!"
if defined HLR set "ARGS=!ARGS! --hlr "!HLR!""
goto :add_freecad

:add_freecad
if defined FREECADCMD if not exist "!FREECADCMD!" (
    echo [WARN] the configured freecadcmd does not exist: !FREECADCMD!
    set "FREECADCMD="
)
set "FC_ON_PATH="
for /f "delims=" %%P in ('where freecadcmd 2^>nul') do set "FC_ON_PATH=%%P"
if defined NEED_FC if not defined FREECADCMD if not defined FC_ON_PATH (
    echo.
    echo [ERROR] FreeCAD ^(freecadcmd.exe^) was not found.
    echo         It builds the parametric .fcstd model.
    echo         Install FreeCAD, then run setup.bat again.
    echo         Download: https://www.freecad.org/downloads.php
    pause
    exit /b 1
)
echo !ARGS! | findstr /i /c:"--freecadcmd" >nul
if errorlevel 1 (
    if defined FREECADCMD (
        set "ARGS=!ARGS! --freecadcmd "!FREECADCMD!""
    ) else (
        echo [WARN] FreeCAD is not configured; trying "freecadcmd" from PATH.
        echo        If the build fails, run setup.bat to set the path.
    )
)
goto :run

:run
echo.
echo Running: python -m step2param.step2param !ARGS!
echo.
"%VPY%" -m step2param.step2param !ARGS!
set "RC=!ERRORLEVEL!"
echo.
if not "!RC!"=="0" (
    echo [FAILED] the program exited with code !RC!
    echo          See the output above for the reason.
    pause
    exit /b !RC!
)
echo Done.
pause
exit /b 0
