@echo off
rem O AI — bring a git checkout up to date before every start (called by "Start O.bat" and launch.cmd with the
rem O folder as the current directory). Mirrors "Launch O.command":
rem   1. git pull --ff-only origin main, synchronously, capped at 20 s (no pull in the background while
rem      Electron starts: the app would run half-old, half-new files).
rem   2. package.json newer than node_modules\.package-lock.json  -> npm install
rem   3. engine\pyproject.toml newer than engine\.venv\.o-installed -> pip install -e engine
rem   4. Playwright's Chromium missing from the engine venv          -> playwright install chromium
rem Every step is best-effort: a failure prints a line and O still starts with what is on disk.
setlocal

if exist ".git" (
  where git >nul 2>nul && (
    powershell -NoProfile -NonInteractive -Command "$p = Start-Process git -ArgumentList 'pull','-q','--ff-only','origin','main' -NoNewWindow -PassThru; if (-not $p.WaitForExit(20000)) { try { $p.Kill() } catch {}; Write-Host '(could not pull the latest O in 20 s; starting the version on disk)' } elseif ($p.ExitCode -ne 0) { Write-Host '(could not pull the latest O; starting the version on disk)' }"
  )
)

rem --- app packages: package.json newer than npm's own lock (PowerShell compares the timestamps; no -nt in cmd)
set "NEED_NPM=0"
if not exist "node_modules\electron\dist\electron.exe" set "NEED_NPM=1"
if "%NEED_NPM%"=="0" (
  powershell -NoProfile -NonInteractive -Command "exit [int](-not (Test-Path 'node_modules\.package-lock.json') -or ((Get-Item 'package.json').LastWriteTimeUtc -gt (Get-Item 'node_modules\.package-lock.json').LastWriteTimeUtc))"
  if errorlevel 1 set "NEED_NPM=1"
)
if "%NEED_NPM%"=="1" (
  echo Installing app packages...
  call npm install --no-audit --no-fund
  if errorlevel 1 echo npm install failed; O starts with the packages on disk.
)

rem --- engine packages + Chromium (only when the engine venv exists: the one-line installer creates it)
set "VENV_PY=engine\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" goto :done
powershell -NoProfile -NonInteractive -Command "exit [int](-not (Test-Path 'engine\.venv\.o-installed') -or ((Get-Item 'engine\pyproject.toml').LastWriteTimeUtc -gt (Get-Item 'engine\.venv\.o-installed').LastWriteTimeUtc))"
if errorlevel 1 (
  echo Updating engine packages...
  "%VENV_PY%" -m pip install -q -e engine && type nul > "engine\.venv\.o-installed"
)
"%VENV_PY%" -c "import os, sys; from playwright.sync_api import sync_playwright; p = sync_playwright().start(); ok = os.path.exists(p.chromium.executable_path); p.stop(); sys.exit(0 if ok else 1)" >nul 2>nul
if errorlevel 1 (
  echo Installing Chromium for the engine...
  "%VENV_PY%" -m playwright install chromium >nul 2>nul
)

:done
endlocal
exit /b 0
