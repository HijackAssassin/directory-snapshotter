@echo off
rem ==========================================================================
rem  Directory Snapshotter's build steps. build.bat (in the project folder) calls:
rem      Code\build-app.bat <version> "<project folder>"
rem  ->  Build\Directory Snapshotter.exe (one file, version.txt bundled in - the update check reads it).
rem  The real work is in build.py (PyInstaller, the icon). Needs Python 3 with PyInstaller (installed here if missing).
rem ==========================================================================
setlocal
set "VERSION=%~1"
if "%VERSION%"=="" echo Run build.bat in the project folder, not this. & exit /b 1
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY echo Python 3 is required: https://www.python.org/downloads/ & exit /b 1
%PY% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo PyInstaller is not installed for this Python - installing it now ...
    %PY% -m pip install --user pyinstaller || exit /b 1
)
%PY% build.py %VERSION%
