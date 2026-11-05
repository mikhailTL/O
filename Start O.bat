@echo off
rem Double-click to start O AI. The first time, it installs what O needs (about a minute).
title O AI
cd /d "%~dp0"

if exist "node_modules\electron\dist\electron.exe" goto run

where npm >nul 2>nul
if errorlevel 1 (
  echo O needs Node.js first. Install the LTS version from https://nodejs.org , then double-click this file again.
  start "" https://nodejs.org
  pause
  exit /b 1
)
echo Setting up O for the first time (this takes a minute)...
call npm install --no-audit --no-fund
if errorlevel 1 (
  echo.
  echo Setup failed. Send the messages above to whoever helps you with O.
  pause
  exit /b 1
)

:run
rem Start O without keeping this window open. If O is already running, this opens its panel.
start "" "node_modules\electron\dist\electron.exe" "%~dp0."
exit /b 0
