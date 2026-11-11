@echo off
rem O AI launcher (Windows). Started hidden by "O AI.vbs" from the Start menu / Desktop shortcut.
rem Pulls the latest O (synchronously, 20 s cap), installs whatever the pull changed (refresh.cmd), then starts
rem the app. If O is already running, this opens its bar.
setlocal
cd /d "%~dp0\..\.."
set "EL=node_modules\electron\dist\electron.exe"
if not exist "%EL%" (
  echo O is not installed completely. Run the installer again:
  echo   irm https://raw.githubusercontent.com/mikhailTL/O/main/install/windows/Install-O.ps1 ^| iex
  pause
  exit /b 1
)
call "install\windows\refresh.cmd"
start "" "%EL%" "%CD%"
exit /b 0
