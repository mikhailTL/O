@echo off
rem Double-click to start O AI. The first time, it installs what O needs (a few minutes).
rem For the full install (Start menu entry, self-update, Claude Code) run in PowerShell:
rem   irm https://raw.githubusercontent.com/mikhailTL/O/main/install/windows/Install-O.ps1 | iex
title O AI
cd /d "%~dp0"

if exist "node_modules\electron\dist\electron.exe" goto engine

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

:engine
rem The engine's Python environment, once (Python 3.10+; skipped quietly when there is no Python: the app
rem runs without the engine and Setup offers to install it).
if exist "engine\.venv\Scripts\python.exe" goto refresh
set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
if not defined PY goto refresh
echo Setting up the engine (a few minutes the first time)...
%PY% -m venv engine\.venv || goto refresh
"engine\.venv\Scripts\python.exe" -m pip install -q --upgrade pip >nul 2>nul
"engine\.venv\Scripts\python.exe" -m pip install -q -e engine && type nul > "engine\.venv\.o-installed"
"engine\.venv\Scripts\python.exe" -m playwright install chromium >nul 2>nul

:refresh
rem Pull the latest O (synchronously, 20 s cap) and install whatever the pull changed, then start it.
rem If O is already running, this opens its panel.
if exist "install\windows\refresh.cmd" call "install\windows\refresh.cmd"
start "" "node_modules\electron\dist\electron.exe" "%~dp0."
exit /b 0
