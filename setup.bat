@echo off
rem ===========================================================================
rem  step2param - setup (Windows)
rem
rem  Installs everything the program needs:
rem    1. checks for Python 3.10+ (64-bit)
rem    2. creates the .venv virtual environment in this folder
rem    3. installs the Python packages from requirements.txt
rem    4. locates freecadcmd.exe (FreeCAD 1.x) and remembers its path
rem
rem  Run it once by double-clicking. To point it at a FreeCAD install,
rem  either drag freecadcmd.exe onto setup.bat, or run:
rem      setup.bat "C:\Program Files\FreeCAD 1.1"
rem ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title step2param - setup

set "PF86=%ProgramFiles(x86)%"

echo ============================================================
echo  step2param - setup
echo  STEP (+ 2D drawing)  -^>  parametric FreeCAD model
echo ============================================================
echo.

rem ---------------------------------------------------------------- Python
set "PYCMD="
for %%V in (3.12 3.11 3.10) do (
    if not defined PYCMD (
        py -%%V -c "import sys,struct; sys.exit(0 if sys.version_info[:2]>=(3,10) and struct.calcsize('P')==8 else 1)" >nul 2>nul
        if not errorlevel 1 set "PYCMD=py -%%V"
    )
)
if not defined PYCMD (
    python -c "import sys,struct; sys.exit(0 if sys.version_info[:2]>=(3,10) and struct.calcsize('P')==8 else 1)" >nul 2>nul
    if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD (
    echo [ERROR] Python 3.10 or newer ^(64-bit^) was not found.
    echo.
    echo   Install Python from https://www.python.org/downloads/windows/
    echo   Tick "Add python.exe to PATH" during the install and run
    echo   setup.bat again.
    goto :fail
)
echo [1/4] Python:
%PYCMD% --version

rem ------------------------------------------------------- virtual environment
echo.
echo [2/4] Virtual environment .venv
if not exist ".venv\Scripts\python.exe" (
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment .venv
        goto :fail
    )
    echo       created
) else (
    echo       already exists - reusing it
)
set "VPY=%~dp0.venv\Scripts\python.exe"

rem ------------------------------------------------------- Python packages
echo.
echo [3/4] Python packages ^(this can take a few minutes, OCP is large^) ...
"%VPY%" -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] Could not update pip.
    goto :fail
)
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Package install failed. Check the internet connection and
    echo         the message above, then run setup.bat again.
    goto :fail
)
"%VPY%" -c "import OCP, numpy, PIL, matplotlib; print('      OCP + numpy + Pillow + matplotlib OK')"
if errorlevel 1 (
    echo [ERROR] The packages did not import. Run setup.bat again.
    goto :fail
)

rem ------------------------------------------------------------- FreeCAD
echo.
echo [4/4] FreeCAD ^(freecadcmd.exe^)
set "FREECADCMD="
if not "%~1"=="" (
    if exist "%~1\bin\freecadcmd.exe" (
        set "FREECADCMD=%~1\bin\freecadcmd.exe"
    ) else (
        if exist "%~1" set "FREECADCMD=%~1"
    )
)
if not defined FREECADCMD call :detect_freecad
if not defined FREECADCMD (
    echo       not found.
    echo.
    echo   step2param needs FreeCAD 1.x to build the .fcstd model.
    echo   Download page: https://www.freecad.org/downloads.php
    echo.
    where winget >nul 2>nul
    if not errorlevel 1 (
        echo   winget is available and can install FreeCAD ^(~470 MB^).
        set /p "YN=  Install FreeCAD now? [y/N]: "
        if /i "!YN!"=="y" (
            winget install -e --id FreeCAD.FreeCAD --accept-package-agreements --accept-source-agreements
            call :detect_freecad
        )
    )
)
if not defined FREECADCMD (
    echo.
    echo   If FreeCAD is already installed somewhere else, type the full
    echo   path to freecadcmd.exe below ^(or press Enter to skip^).
    set /p "FREECADCMD=  freecadcmd.exe path: "
)
if defined FREECADCMD (
    set "FREECADCMD=!FREECADCMD:"=!"
    if not exist "!FREECADCMD!" (
        echo [ERROR] Not found: !FREECADCMD!
        set "FREECADCMD="
    )
)
if defined FREECADCMD (
    > ".venv\freecadcmd.txt" echo !FREECADCMD!
    echo       using: !FREECADCMD!
) else (
    echo.
    echo [WARN] FreeCAD was not configured, so the model build will fail.
    echo        Install FreeCAD and run setup.bat again to fix this.
)

echo.
echo ============================================================
echo  Setup complete.
echo.
echo  Run the program:   run.bat
echo  Run an example:    run.bat examples\output_flange.step
echo  Command-line help: run.bat --help
echo ============================================================
pause
exit /b 0

rem ------------------------------------------------------------ subroutines
:detect_freecad
set "FREECADCMD="
if defined ProgramFiles (
    for /f "delims=" %%D in ('dir /b /ad /on "%ProgramFiles%\FreeCAD*" 2^>nul') do (
        if exist "%ProgramFiles%\%%D\bin\freecadcmd.exe" set "FREECADCMD=%ProgramFiles%\%%D\bin\freecadcmd.exe"
    )
)
if not defined FREECADCMD if defined PF86 (
    for /f "delims=" %%D in ('dir /b /ad /on "%PF86%\FreeCAD*" 2^>nul') do (
        if exist "%PF86%\%%D\bin\freecadcmd.exe" set "FREECADCMD=%PF86%\%%D\bin\freecadcmd.exe"
    )
)
if not defined FREECADCMD if defined LOCALAPPDATA (
    for /f "delims=" %%D in ('dir /b /ad /on "%LOCALAPPDATA%\Programs\FreeCAD*" 2^>nul') do (
        if exist "%LOCALAPPDATA%\Programs\%%D\bin\freecadcmd.exe" set "FREECADCMD=%LOCALAPPDATA%\Programs\%%D\bin\freecadcmd.exe"
    )
)
rem FreeCAD can live on any drive, e.g. E:\Program Files\FreeCAD 1.1
if not defined FREECADCMD (
    for %%X in (C D E F G H I J) do (
        if not defined FREECADCMD (
            for /f "delims=" %%D in ('dir /b /ad /on "%%X:\FreeCAD*" 2^>nul') do (
                if exist "%%X:\%%D\bin\freecadcmd.exe" set "FREECADCMD=%%X:\%%D\bin\freecadcmd.exe"
            )
            for /f "delims=" %%D in ('dir /b /ad /on "%%X:\Program Files\FreeCAD*" 2^>nul') do (
                if exist "%%X:\Program Files\%%D\bin\freecadcmd.exe" set "FREECADCMD=%%X:\Program Files\%%D\bin\freecadcmd.exe"
            )
        )
    )
)
if not defined FREECADCMD (
    for /f "delims=" %%P in ('where freecadcmd 2^>nul') do set "FREECADCMD=%%P"
)
exit /b 0

:fail
echo.
echo Setup failed. Fix the problem above and run setup.bat again.
pause
exit /b 1
