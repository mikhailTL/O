# O AI installer — Windows (PowerShell 5.1 or 7).
# Idempotent: safe to run again at any time. Each step prints "✓ name" or "✗ name — what to do".
# Exit code = number of failed required steps. Every step is required except the Docker presence check.
# Required engine packages: mcp, e2b, crewai, browser-use (in engine\pyproject.toml) and open-interpreter
# (its own step: its pins conflict with browser-use, see Step-OpenInterpreter), and the ambient extra.
# vLLM does not run locally on Windows: the installer asks for the URL of a remote or Docker vLLM
# server and writes it to O's settings (vllmUrl).
#
# Run with:  powershell -ExecutionPolicy Bypass -File install\install.ps1 [-DryRun] [-VllmUrl URL]
#   -DryRun / --dry-run       print the commands instead of running them (same as $env:O_INSTALL_DRY = '1')
#   -VllmUrl / --vllm-url URL OpenAI-compatible vLLM endpoint, e.g. http://gpu-box:8000/v1; no prompt is shown

$ErrorActionPreference = 'Continue'

# Flags are parsed by hand so both PowerShell and install.sh spellings work.
$DryRun = $false
$VllmUrl = ''
for ($i = 0; $i -lt $args.Count; $i++) {
  $a = [string]$args[$i]
  switch -Regex ($a) {
    '^(-DryRun|--dry-run)$' { $DryRun = $true }
    '^--vllm-url=(.+)$' { $VllmUrl = $Matches[1] }
    '^(-VllmUrl|--vllm-url)$' {
      if ($i + 1 -ge $args.Count) { Write-Host "✗ $a needs a URL"; exit 1 }
      $i++; $VllmUrl = [string]$args[$i]
    }
    '^(-h|--help|-Help)$' {
      Write-Host 'Usage: powershell -ExecutionPolicy Bypass -File install\install.ps1 [-DryRun] [-VllmUrl URL]'
      exit 0
    }
    default { Write-Host "✗ unknown option '$a'"; exit 1 }
  }
}

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

$Dry = ($DryRun -or $env:O_INSTALL_DRY -eq '1')
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

# O's settings file (Electron userData of app "O AI"); the installer only merges keys into it.
$SettingsFile = Join-Path $env:APPDATA 'O AI\o-settings.json'

function Set-OSetting($key, $value) {
  if ($Dry) {
    Write-Host "  [dry] set $key=$value in $SettingsFile"
    return $true
  }
  try {
    $data = [ordered]@{}
    if (Test-Path $SettingsFile) {
      $obj = Get-Content -Raw -Path $SettingsFile | ConvertFrom-Json
      foreach ($p in $obj.PSObject.Properties) { $data[$p.Name] = $p.Value }
    }
    $data[$key] = $value
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SettingsFile) | Out-Null
    $tmp = "$SettingsFile.tmp"
    [System.IO.File]::WriteAllText($tmp, ($data | ConvertTo-Json -Depth 20), (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -Force -Path $tmp -Destination $SettingsFile
    return $true
  } catch {
    return $false
  }
}

# vLLM: not supported locally on Windows. A remote or Docker vLLM URL is required.
$VllmDocker = 'docker run --gpus all -p 8000:8000 vllm/vllm-openai --model meta-llama/Meta-Llama-3-8B-Instruct (on a machine with an NVIDIA GPU)'
function Step-Vllm {
  $url = $VllmUrl
  if (-not $url -and -not $Dry -and [Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
    Write-Host "  vLLM cannot run locally on Windows. Start one elsewhere, e.g.: $VllmDocker"
    $url = Read-Host '  vLLM URL (OpenAI-compatible, e.g. http://gpu-box:8000/v1)'
  }
  if (-not $url) {
    if ($Dry) {
      Write-Host '  [dry] prompt for vLLM URL (or pass -VllmUrl URL), then save it as vllmUrl'
      Ok 'vllm (remote URL)'
      return
    }
    Fail 'vllm' "no vLLM URL given; start one with '$VllmDocker' and run this script again with -VllmUrl http://<host>:8000/v1"
    return
  }
  if ($url -notmatch '^https?://') {
    Fail 'vllm' "'$url' is not an http(s) URL; pass e.g. -VllmUrl http://gpu-box:8000/v1"
    return
  }
  $url = $url.TrimEnd('/')
  if (-not (Set-OSetting 'vllmUrl' $url)) {
    Fail 'vllm' "could not write vllmUrl to $SettingsFile; set Settings → Engine → vLLM URL to $url by hand"
    return
  }
  if ($Dry) {
    Write-Host "  [dry] GET $url/models"
    Ok "vllm (remote URL $url)"
    return
  }
  try {
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Uri "$url/models" | Out-Null
    Ok "vllm (remote URL $url saved as vllmUrl, reachable)"
  } catch {
    Fail 'vllm' "saved vllmUrl=$url but $url/models did not answer; start the server ($VllmDocker) and run this script again"
  }
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

# Required engine packages: installed by 'pip install -e engine' (engine\pyproject.toml); verified by import.
# mcp = MCP tool routing, e2b = E2B containment, crewai = three-role swarm, browser_use = headless web agent.
$RequiredModules = [ordered]@{ 'mcp' = 'mcp'; 'e2b' = 'e2b'; 'crewai' = 'crewai'; 'browser_use' = 'browser-use' }
function Step-RequiredPackages {
  if (-not (Test-Path $VenvPy) -and -not $Dry) {
    Fail 'engine packages' 'skipped: engine venv missing'
    return
  }
  foreach ($mod in $RequiredModules.Keys) {
    $pkg = $RequiredModules[$mod]
    if ($Dry) {
      Write-Host "  [dry] $VenvPy -c 'import $mod'"
      Ok $pkg
      continue
    }
    & $VenvPy -c "import $mod" *> $null
    if ($LASTEXITCODE -eq 0) {
      Ok $pkg
    } else {
      Fail $pkg "run '$VenvPy -m pip install -e $(Join-Path $Root 'engine')' and read its errors (then '$VenvPy -c `"import $mod`"')"
    }
  }
}

# open-interpreter: required, installed on its own. Its release pins anthropic<0.38 and psutil<6, while
# browser-use needs psutil>=7 and a newer anthropic, so pip cannot resolve both. It is installed with
# --no-deps plus its runtime dependencies unpinned; setuptools<81 keeps pkg_resources, which it imports.
$OiSpec = 'open-interpreter>=0.4.3,<0.5'
$OiDeps = @('astor', 'git-python', 'google-generativeai', 'html2image', 'html2text', 'inquirer', 'ipykernel', 'jupyter-client', 'litellm', 'matplotlib', 'nltk', 'platformdirs', 'pyperclip', 'pyyaml', 'selenium', 'send2trash', 'shortuuid', 'six', 'tiktoken', 'tokentrim', 'toml', 'webdriver-manager', 'wget', 'yaspin', 'pyreadline3', 'setuptools<81')
function Step-OpenInterpreter {
  if (-not (Test-Path $VenvPy) -and -not $Dry) {
    Fail 'open-interpreter' 'skipped: engine venv missing'
    return
  }
  $fix = "run '$VenvPy -m pip install --no-deps `"$OiSpec`"' and '$VenvPy -m pip install $($OiDeps -join ' ')' and read their errors"
  if ((Invoke-Step $VenvPy @('-m', 'pip', 'install', '--no-deps', $OiSpec)) -and (Invoke-Step $VenvPy (@('-m', 'pip', 'install') + $OiDeps))) {
    if ($Dry) {
      Write-Host "  [dry] $VenvPy -c 'from interpreter import interpreter'"
      Ok 'open-interpreter'
      return
    }
    & $VenvPy -W ignore -c 'from interpreter import interpreter' *> $null
    if ($LASTEXITCODE -eq 0) {
      Ok 'open-interpreter'
    } else {
      Fail 'open-interpreter' "installed but 'from interpreter import interpreter' fails; $fix"
    }
  } else {
    Fail 'open-interpreter' $fix
  }
}

# Ambient layer (required): sensors, presence, isolation. 'pip install -e "engine[ambient]"' (the ambient
# extra in engine\pyproject.toml), then verified by import. microsandbox on Windows needs WHP
# (Windows Hypervisor Platform); without it headless jobs fall back to Docker or the local sandbox.
$AmbientModules = [ordered]@{ 'cv2' = 'opencv-python'; 'sounddevice' = 'sounddevice'; 'paho.mqtt.client' = 'paho-mqtt'; 'websockets' = 'websockets'; 'redis' = 'redis'; 'microsandbox' = 'microsandbox'; 'rppg' = 'open-rppg'; 'mediapipe' = 'mediapipe' }
function Step-Ambient {
  if (-not (Test-Path $VenvPy) -and -not $Dry) {
    Fail 'ambient' 'skipped: engine venv missing'
    return
  }
  $spec = (Join-Path $Root 'engine') + '[ambient]'
  $fix = "run '$VenvPy -m pip install -e `"$spec`"' and read its errors"
  if (-not (Invoke-Step $VenvPy @('-m', 'pip', 'install', '-e', $spec))) {
    Fail 'ambient' $fix
    return
  }
  if ($Dry) {
    Write-Host "  [dry] $VenvPy -c 'import $($AmbientModules.Keys -join ', ')'"
    Ok 'ambient (pip install -e engine[ambient])'
    return
  }
  $missing = @()
  foreach ($mod in $AmbientModules.Keys) {
    & $VenvPy -W ignore -c "import $mod" *> $null
    if ($LASTEXITCODE -ne 0) { $missing += $AmbientModules[$mod] }
  }
  if ($missing.Count -gt 0) {
    Fail 'ambient' "these do not import: $($missing -join ', '); $fix"
    return
  }
  Ok 'ambient (pip install -e engine[ambient])'
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
Step-RequiredPackages
Step-OpenInterpreter
Step-Ambient
Step-Docker
Step-EngineCheck

Write-Host ''
Write-Host "Set Settings → Engine → Python to: $VenvPy"
Write-Host "If Claude Code was just installed, run 'claude' once in a terminal to log in (your Claude subscription powers O)."
Write-Host ''

if ($script:Failed.Count -eq 0) {
  Write-Host 'All steps succeeded.'
  exit 0
}

Write-Host "Failed steps ($($script:Failed.Count)):"
foreach ($s in $script:Failed) { Write-Host "  - $s" }
exit $script:Failed.Count
