@echo off
rem O AI launcher (Windows). Started hidden by "O AI.vbs" from the Start menu / Desktop shortcut.
rem Pulls the latest O in the background, then starts the app. If O is already running, this opens its bar.
setlocal
cd /d "%~dp0\..\.."
if exist ".git" start "" /b /min cmd /c "git pull -q --ff-only origin main >nul 2>&1"
set "EL=node_modules\electron\dist\electron.exe"
if not exist "%EL%" (
  echo O is not installed completely. Run the installer again:
  echo   irm https://raw.githubusercontent.com/mikhailTL/O/main/install/windows/Install-O.ps1 ^| iex
  pause
  exit /b 1
)
start "" "%EL%" "%CD%"
exit /b 0
