# O AI installer — Windows (PowerShell 5.1 or 7).
# Idempotent: safe to run again at any time. Each step prints "✓ name" or "✗ name — what to do".
# Exit code = number of failed required steps. vLLM is not supported locally on Windows and
# counts as failed-but-continue; the alternative is printed. Phase 2 packages and Docker are optional
# and never count as failures.
# Set $env:O_INSTALL_DRY = '1' to print the commands instead of running them.
# Run with:  powershell -ExecutionPolicy Bypass -File install\install.ps1

$ErrorActionPreference = 'Continue'

# ---------- locate repo root (works from repo root or from install\) ----------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $ScriptDir 'package.json')) {
  $Root = $ScriptDir
} elseif (Test-Path (Join-Path (Split-Path -Parent $ScriptDir) 'package.json')) {
  $Root = Split-Path -Parent $ScriptDir
} else {
  Write-Host '✗ repo root — run this script from the O repo root or from its install\ directory'
  exit 1
}
Set-Location $Root

$Dry = ($env:O_INSTALL_DRY -eq '1')
$Venv = Join-Path $Root 'engine\.venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$VenvEngine = Join-Path $Venv 'Scripts\o-engine.exe'
$script:Failed = @()
$script:Python = $null

# ---------- helpers ----------
function Ok($name) { Write-Host "✓ $name" }
function Fail($name, $what) { Write-Host "✗ $name — $what"; $script:Failed += $name }

# Invoke-Step <exe> <args...> : runs the command (or prints it in dry mode); returns $true on exit code 0
function Invoke-Step {
  param([string]$Exe, [string[]]$ArgList)
  if ($Dry) {
    Write-Host "  [dry] $Exe $($ArgList -join ' ')"
    return $true
  }
  & $Exe @ArgList
  if ($LASTEXITCODE -ne 0) { return $false }
  return $true
}

function Test-Command($name) {
  return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# Get-VersionFrom <text> : first dotted version in text as [version], or $null
function Get-VersionFrom($text) {
  if ($null -eq $text) { return $null }
  $m = [regex]::Match([string]$text, '(\d+)\.(\d+)(\.(\d+))?')
  if (-not $m.Success) { return $null }
  $v = $m.Groups[1].Value + '.' + $m.Groups[2].Value
  if ($m.Groups[4].Success) { $v = $v + '.' + $m.Groups[4].Value } else { $v = $v + '.0' }
  return [version]$v
}

# Find-Python : first python / py -3 that is >= 3.10; returns the launcher as a string array
function Find-Python {
  $candidates = @(
    @('python'),
    @('python3'),
    @('py', '-3')
  )
  foreach ($c in $candidates) {
    if (-not (Test-Command $c[0])) { continue }
    $exe = $c[0]
    $pre = @()
    if ($c.Count -gt 1) { $pre = $c[1..($c.Count - 1)] }
    $out = & $exe @pre -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>$null
    if ($LASTEXITCODE -ne 0) { continue }
    $v = Get-VersionFrom $out
    if ($null -eq $v) { continue }
    if ($v -ge [version]'3.10.0') { return ,$c }
  }
  return $null
}

# ---------- steps ----------
function Step-Node {
  if (-not (Test-Command 'node')) {
    Fail 'node' 'install Node.js 18 or newer (LTS) from https://nodejs.org and run this script again'
    return
  }
  $v = Get-VersionFrom (& node --version 2>$null)
  if ($null -ne $v -and $v -ge [version]'18.0.0') {
    Ok "node $v"
  } else {
    Fail 'node' "found $v; install Node.js 18 or newer (LTS) from https://nodejs.org"
  }
}

function Step-NpmInstall {
  if (Test-Path (Join-Path $Root 'node_modules\electron')) {
    Ok 'npm install (already present)'
    return
  }
  if (-not (Test-Command 'npm')) {
    Fail 'npm install' 'npm not found; install Node.js from https://nodejs.org'
    return
  }
  if (Invoke-Step 'npm.cmd' @('install', '--no-audit', '--no-fund')) {
    Ok 'npm install'
  } else {
    Fail 'npm install' "re-run 'npm install --no-audit --no-fund' in $Root and read its errors"
  }
}

function Step-ClaudeCli {
  if (Test-Command 'claude') {
    $v = & claude --version 2>$null
    if ($LASTEXITCODE -eq 0) {
      Ok "claude code cli ($v)"
      return
    }
  }
  if (-not (Test-Command 'npm')) {
    Fail 'claude code cli' "npm not found; install Node.js then run 'npm install -g @anthropic-ai/claude-code'"
    return
  }
  if (Invoke-Step 'npm.cmd' @('install', '-g', '@anthropic-ai/claude-code')) {
    Ok "claude code cli (installed; run 'claude' once in a terminal to log in)"
  } else {
    Fail 'claude code cli' "run 'npm install -g @anthropic-ai/claude-code', then run 'claude' once to log in"
  }
}

function Step-Python {
  $script:Python = Find-Python
  if ($null -ne $script:Python) {
    $exe = $script:Python[0]
    $pre = @()
    if ($script:Python.Count -gt 1) { $pre = $script:Python[1..($script:Python.Count - 1)] }
    $v = & $exe @pre --version 2>&1
    Ok "python ($v)"
  } else {
    Fail 'python' 'install Python 3.10 or newer from https://www.python.org/downloads/ (tick "Add python.exe to PATH") and run this script again'
  }
}

function Step-VenvEngine {
  if ($null -eq $script:Python) {
    Fail 'engine venv' 'skipped: Python 3.10+ not found'
    return
  }
  if (-not (Test-Path $VenvPy)) {
    $exe = $script:Python[0]
    $pre = @()
    if ($script:Python.Count -gt 1) { $pre = $script:Python[1..($script:Python.Count - 1)] }
    $venvArgs = $pre + @('-m', 'venv', $Venv)
    if (-not (Invoke-Step $exe $venvArgs)) {
      Fail 'engine venv' "could not create $Venv"
      return
    }
  }
  if ($Dry -and -not (Test-Path $VenvPy)) {
    Write-Host "  [dry] $VenvPy -m pip install -e $(Join-Path $Root 'engine')"
    Ok 'engine venv + pip install -e engine'
    return
  }
  Invoke-Step $VenvPy @('-m', 'pip', 'install', '--upgrade', 'pip') | Out-Null
  if (Invoke-Step $VenvPy @('-m', 'pip', 'install', '-e', (Join-Path $Root 'engine'))) {
    Ok 'engine venv + pip install -e engine'
  } else {
    Fail 'engine venv' "run '$VenvPy -m pip install -e $(Join-Path $Root 'engine')' and read its errors"
  }
}

function Step-Playwright {
  if (-not (Test-Path $VenvPy) -and -not $Dry) {
    Fail 'playwright chromium' 'skipped: engine venv missing'
    return
  }
  if (Invoke-Step $VenvPy @('-m', 'playwright', 'install', 'chromium')) {
    Ok 'playwright chromium'
  } else {
    Fail 'playwright chromium' "run '$VenvPy -m playwright install chromium'"
  }
}

# vLLM: not supported locally on Windows. Point O at a remote vLLM instead.
function Step-Vllm {
  Fail 'vllm' "not supported locally on Windows; run vLLM on a Linux box with CUDA or in Docker 'vllm/vllm-openai' and set Settings → Engine → vLLM URL"
}

# screenpipe: published installer one-liner from https://github.com/mediar-ai/screenpipe
# If this command stops working, check that page for the current install command.
function Step-Screenpipe {
  if (Test-Command 'screenpipe') {
    Ok 'screenpipe (already installed)'
    return
  }
  $cmd = 'irm https://raw.githubusercontent.com/mediar-ai/screenpipe/main/install.ps1 | iex'
  if ($Dry) {
    Write-Host "  [dry] $cmd"
    Ok 'screenpipe'
    return
  }
  try {
    Invoke-Expression (Invoke-RestMethod 'https://raw.githubusercontent.com/mediar-ai/screenpipe/main/install.ps1')
    Ok "screenpipe (open a new terminal if 'screenpipe' is not on PATH yet)"
  } catch {
    Fail 'screenpipe' "run '$cmd'; if it fails, see https://github.com/mediar-ai/screenpipe for the current installer"
  }
}

# Xvfb: Linux only. Windows: headless-only.
function Step-Xvfb {
  Ok 'xvfb — isolated display: headless-only on this OS'
}

# Phase 2 packages: MCP routing, Open Interpreter runner, E2B containment, crewAI swarm, browser-use.
# Each is optional; the engine falls back without it. One pip call per package, none fatal.
$Phase2Pkgs = @('mcp', 'open-interpreter', 'e2b', 'crewai', 'browser-use')
function Step-Phase2Packages {
  if (-not (Test-Path $VenvPy) -and -not $Dry) {
    Fail 'phase 2 packages' 'skipped: engine venv missing'
    return
  }
  $missing = @()
  foreach ($pkg in $Phase2Pkgs) {
    if ($Dry) {
      Write-Host "  [dry] $VenvPy -m pip install $pkg"
      Ok "phase 2: $pkg"
      continue
    }
    & $VenvPy -m pip install $pkg *> $null
    if ($LASTEXITCODE -eq 0) {
      Ok "phase 2: $pkg"
    } else {
      Write-Host "  ✗ phase 2: $pkg — optional; run '$VenvPy -m pip install $pkg' later if you want it"
      $missing += $pkg
    }
  }
  if ($missing.Count -eq 0) {
    Ok 'phase 2 packages'
  } else {
    Ok "phase 2 packages ($($missing.Count) optional package(s) not installed: $($missing -join ' '))"
  }
}

# Docker: used for risk:"high" steps (docker run --rm --network <dockerNetwork>, default bridge). Reported, never installed.
function Step-Docker {
  if (Test-Command 'docker') {
    Ok 'docker (found; the engine uses it for high-risk steps when the daemon is running)'
  } else {
    Write-Host '✗ docker — optional; install Docker Desktop from https://docs.docker.com/get-docker/ for disposable high-risk sandboxes, or set highRiskSandbox to e2b/local'
  }
}

function Step-EngineCheck {
  if ($Dry) {
    Write-Host "  [dry] $VenvEngine check"
    Ok 'engine check'
    return
  }
  if (-not (Test-Path $VenvEngine)) {
    Fail 'engine check' 'engine not installed yet (run this script again after fixing the engine venv step)'
    return
  }
  & $VenvEngine check
  if ($LASTEXITCODE -eq 0) {
    Ok 'engine check'
  } else {
    Fail 'engine check' "'$VenvEngine check' reported problems above; fix them and run it again"
  }
}

# ---------- main ----------
Write-Host "O AI install — Windows — repo: $Root"
if ($Dry) { Write-Host '(dry run: commands are printed, not executed)' }
Write-Host ''

Step-Node
Step-NpmInstall
Step-ClaudeCli
Step-Python
Step-VenvEngine
Step-Playwright
Step-Vllm
Step-Screenpipe
Step-Xvfb
Step-Phase2Packages
Step-Docker
Step-EngineCheck

Write-Host ''
Write-Host "Set Settings → Engine → Python to: $VenvPy"
Write-Host "If Claude Code was just installed, run 'claude' once in a terminal to log in."
Write-Host ''

if ($script:Failed.Count -eq 0) {
  Write-Host 'All steps succeeded.'
  exit 0
}

Write-Host "Failed steps ($($script:Failed.Count)):"
foreach ($s in $script:Failed) { Write-Host "  - $s" }
exit $script:Failed.Count
