# O AI — Windows installer. One line in PowerShell:
#
#   irm https://raw.githubusercontent.com/mikhailTL/O/main/install/windows/Install-O.ps1 | iex
#
# What it does (idempotent, safe to re-run): installs Git, Node LTS and Python 3.12 with winget when they are
# missing, clones O to %LOCALAPPDATA%\O (or pulls if it is already there), installs the app's npm packages and
# the engine's Python packages into engine\.venv, builds the "O AI" launcher (hidden console, pulls the latest
# O before every start, so the app updates itself), puts "O AI" in the Start menu and on the Desktop with its
# icon, and starts it. Claude Code is installed too (npm) and you are asked to log in once.
#
# Flags: -NoStart (don't launch at the end), -Dir <path> (install somewhere else), -Ref <branch> (default main).
[CmdletBinding()]
param(
  [string]$Dir = (Join-Path $env:LOCALAPPDATA 'O'),
  [string]$Ref = 'main',
  [string]$Repo = 'https://github.com/mikhailTL/O.git',
  [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# npm/claude ship as .ps1 shims that a Restricted execution policy refuses to run; allow scripts for this process
# only (no admin needed) and call the .cmd shims directly.
try { Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force } catch {}
function Npm { & npm.cmd @args }

function Say($s) { Write-Host "  $s" }
function Ok($s) { Write-Host "  + $s" -ForegroundColor Green }
function Warn($s) { Write-Host "  ! $s" -ForegroundColor Yellow }
function Has($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Winget-Install($id, $name) {
  if (-not (Has 'winget')) { throw "winget is not available. Install '$name' by hand, then run this again." }
  Say "Installing $name..."
  & winget install -e --id $id --accept-source-agreements --accept-package-agreements --silent | Out-Null
  Refresh-Path
}

Write-Host ''
Write-Host '  O AI' -ForegroundColor White
Write-Host '  ----'

# ---- 1. prerequisites -------------------------------------------------------------------------------------
if (-not (Has 'git')) { Winget-Install 'Git.Git' 'Git' }
Ok "Git $((git --version) -replace 'git version ','')"

$nodeOk = $false
if (Has 'node') { $v = (node -v) -replace '^v',''; if ([version]($v.Split('.')[0..1] -join '.') -ge [version]'18.0') { $nodeOk = $true } }
if (-not $nodeOk) { Winget-Install 'OpenJS.NodeJS.LTS' 'Node.js LTS' }
Ok "Node $(node -v)"

# Python >= 3.10: prefer the py launcher, then python3/python; install 3.12 when nothing fits.
function Find-Python {
  foreach ($c in @(@('py', '-3.12'), @('py', '-3.13'), @('py', '-3.11'), @('py', '-3.10'), @('python3'), @('python'))) {
    if (-not (Has $c[0])) { continue }
    try {
      $rest = @(); if ($c.Count -gt 1) { $rest = @($c[1..($c.Count - 1)]) }
      $out = & $c[0] @($rest + @('-c', 'import sys; print(sys.version_info[0], sys.version_info[1])')) 2>$null
      if ($LASTEXITCODE -eq 0 -and $out) {
        $p = $out.Trim().Split(' '); if ([int]$p[0] -eq 3 -and [int]$p[1] -ge 10) { return , $c }
      }
    } catch {}
  }
  return $null
}
$py = Find-Python
if (-not $py) { Winget-Install 'Python.Python.3.12' 'Python 3.12'; $py = Find-Python }
if (-not $py) { throw 'Python 3.10+ was not found after installing. Open a new PowerShell window and run this again.' }
$PyExe = $py[0]
$PyArgs = @(); if ($py.Count -gt 1) { $PyArgs = @($py[1..($py.Count - 1)]) }
function Invoke-Py { param([string[]]$Rest) & $PyExe @($PyArgs + $Rest) }
Ok "Python $(Invoke-Py @('-c', 'import sys; print(sys.version.split()[0])'))"

# ---- 2. the code -------------------------------------------------------------------------------------------
if (Test-Path (Join-Path $Dir '.git')) {
  Say "Updating O in $Dir..."
  # A fast-forward pull, never a reset: local changes are kept (and reported), so nothing of the person's is
  # thrown away by re-running the installer.
  $dirty = (git -C $Dir status --porcelain --untracked-files=no) -join "`n"
  if ($dirty) { Warn "O's folder has local changes; they are kept, but the pull may not apply:`n$dirty" }
  git -C $Dir pull -q --ff-only origin $Ref
  if ($LASTEXITCODE -ne 0) { Warn "Could not pull the latest O (git pull --ff-only origin $Ref); continuing with the version on disk." }
} else {
  if (Test-Path $Dir) { Warn "$Dir exists but is not a git checkout; moving it to $Dir.old"; Move-Item $Dir "$Dir.old" -Force }
  Say "Downloading O to $Dir..."
  git clone -q --depth=1 --branch $Ref $Repo $Dir
}
Ok "O $(git -C $Dir rev-parse --short HEAD)"

# ---- 3. app packages ---------------------------------------------------------------------------------------
Push-Location $Dir
try {
  Say 'Installing app packages (npm)...'
  Npm install --no-audit --no-fund --loglevel=error | Out-Null
  if (-not (Test-Path 'node_modules\electron\dist\electron.exe')) { throw 'Electron did not install (node_modules\electron\dist\electron.exe is missing).' }
  Ok 'App packages'

  if (-not (Has 'claude') -and -not (Has 'claude.cmd')) {
    Say 'Installing Claude Code...'
    Npm install -g @anthropic-ai/claude-code --loglevel=error | Out-Null
    Refresh-Path
  }
  if (Has 'claude.cmd') { Ok "Claude Code $((& claude.cmd --version 2>$null) | Select-Object -First 1)" } elseif (Has 'claude') { Ok 'Claude Code' } else { Warn 'Claude Code did not install; O will offer to install it in Setup.' }

  # ---- 4. engine -------------------------------------------------------------------------------------------
  $venvPy = 'engine\.venv\Scripts\python.exe'
  if (-not (Test-Path $venvPy)) {
    Say 'Creating the engine environment...'
    Invoke-Py @('-m', 'venv', 'engine\.venv')
  }
  Say 'Installing engine packages (this is the slow step, a few minutes the first time)...'
  & $venvPy -m pip install -q --upgrade pip
  & $venvPy -m pip install -q -e engine
  & $venvPy -c 'import o_engine' 2>$null
  if ($LASTEXITCODE -ne 0) { throw 'The engine did not install (import o_engine failed). Scroll up for the pip error.' }
  # The launchers compare this marker with engine\pyproject.toml before running pip again.
  New-Item -ItemType File -Force -Path 'engine\.venv\.o-installed' | Out-Null
  Ok 'Engine'
  # Playwright's Chromium for the engine's browser tools (non-fatal: the engine installs it on first use otherwise).
  Say 'Installing Chromium for the engine...'
  try {
    & $venvPy -m playwright install chromium 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok 'Chromium' } else { Warn 'Chromium did not install now; the engine will fetch it when it first needs it.' }
  } catch { Warn "Chromium did not install now ($($_.Exception.Message)); the engine will fetch it when it first needs it." }

  # ---- 5. launcher + shortcuts -----------------------------------------------------------------------------
  $launcher = Join-Path $Dir 'install\windows\launch.cmd'
  $vbs = Join-Path $Dir 'install\windows\O AI.vbs'
  $ico = Join-Path $Dir 'assets\o.ico'
  $ws = New-Object -ComObject WScript.Shell
  foreach ($where in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
    $lnk = $ws.CreateShortcut((Join-Path $where 'O AI.lnk'))
    $lnk.TargetPath = 'wscript.exe'
    $lnk.Arguments = '"' + $vbs + '"'
    $lnk.WorkingDirectory = $Dir
    $lnk.IconLocation = "$ico,0"
    $lnk.Description = 'O AI'
    $lnk.Save()
  }
  Ok 'O AI is in the Start menu and on the Desktop'

  # ---- 6. settings: engine python, so the app never has to look for it --------------------------------------
  $cfgDir = Join-Path $env:APPDATA 'O AI'
  New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
  $cfg = Join-Path $cfgDir 'o-settings.json'
  $obj = New-Object PSObject
  if (Test-Path $cfg) { try { $obj = Get-Content $cfg -Raw | ConvertFrom-Json } catch { $obj = New-Object PSObject } }
  if ($null -eq $obj) { $obj = New-Object PSObject }
  $obj | Add-Member -NotePropertyName enginePython -NotePropertyValue (Join-Path $Dir $venvPy) -Force
  ($obj | ConvertTo-Json -Depth 8) | Set-Content -Encoding UTF8 $cfg
  Ok 'Settings'
} finally { Pop-Location }

Write-Host ''
Ok 'Installed. Open "O AI" from the Start menu any time; it updates itself on every start.'
if (-not $NoStart) { Start-Process wscript.exe -ArgumentList ('"' + (Join-Path $Dir 'install\windows\O AI.vbs') + '"') -WorkingDirectory $Dir }
