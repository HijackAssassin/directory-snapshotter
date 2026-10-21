@echo off
rem ==========================================================================
rem  Directory Snapshotter's download. Website\"archive this build.bat" calls:
rem      Code\package-app.bat <version> "<project folder>"
rem  It is one exe, so the download is that exe with the version in its name:
rem      Website\Directory Snapshotter\Directory Snapshotter.exe  ->  Website\Directory-Snapshotter-V<version>.exe
rem ==========================================================================
setlocal
set "VERSION=%~1"
set "WEB=%~2\Website"
copy /y "%WEB%\Directory Snapshotter\Directory Snapshotter.exe" "%WEB%\Directory-Snapshotter-V%VERSION%.exe" >nul
