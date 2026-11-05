#!/usr/bin/env bash
# O AI installer — Linux and macOS.
# Idempotent: safe to run again at any time. Each step prints "✓ name" or "✗ name — what to do".
# Exit code = number of failed required steps. Every step is required except the Docker presence check.
# Required engine packages: mcp, e2b, crewai, browser-use (in engine/pyproject.toml) and open-interpreter
# (its own step: its pins conflict with browser-use, see step_open_interpreter).
# vLLM: installed locally on Linux with an NVIDIA GPU; elsewhere the installer asks for the URL of a
# remote or Docker vLLM server and writes it to O's settings (vllmUrl).
#
# Usage: bash install/install.sh [--dry-run] [--vllm-url URL]
#   --dry-run       print the commands instead of running them (same as O_INSTALL_DRY=1)
#   --vllm-url URL  OpenAI-compatible vLLM endpoint, e.g. http://gpu-box:8000/v1; no prompt is shown
set -u

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }

DRY="${O_INSTALL_DRY:-0}"
VLLM_URL_ARG=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --vllm-url) shift; VLLM_URL_ARG="${1:-}"; [ -n "$VLLM_URL_ARG" ] || { echo "✗ --vllm-url needs a URL"; exit 1; } ;;
    --vllm-url=*) VLLM_URL_ARG="${1#--vllm-url=}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "✗ unknown option '$1'"; usage; exit 1 ;;
  esac
  shift
done

# ---------- locate repo root (works from repo root or from install/) ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/package.json" ]; then
  ROOT="$SCRIPT_DIR"
elif [ -f "$SCRIPT_DIR/../package.json" ]; then
  ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  echo "✗ repo root — run this script from the O repo root or from its install/ directory"
  exit 1
fi
cd "$ROOT" || exit 1

OS="$(uname -s)"
VENV="$ROOT/engine/.venv"
VENV_PY="$VENV/bin/python"
VENV_PIP="$VENV/bin/pip"
FAILED=()

# ---------- helpers ----------
ok()   { echo "✓ $1"; }
fail() { echo "✗ $1 — $2"; FAILED+=("$1"); }

# run <cmd...> : runs the command, or prints it in DRY mode
run() {
  if [ "$DRY" = "1" ]; then
    echo "  [dry] $*"
    return 0
  fi
  "$@"
}

# sudo_run <cmd...> : like run, with sudo when not already root
sudo_run() {
  if [ "$(id -u)" = "0" ]; then
    run "$@"
  elif command -v sudo >/dev/null 2>&1; then
    run sudo "$@"
  else
    echo "  sudo not found; cannot run: $*"
    return 1
  fi
}

# version_ge <have> <want> : compares dotted versions numerically
version_ge() {
  local have="$1" want="$2"
  local IFS=.
  local -a h w
  read -r -a h <<< "$have"
  read -r -a w <<< "$want"
  local i
  for i in 0 1 2; do
    local a="${h[$i]:-0}" b="${w[$i]:-0}"
    a="${a//[^0-9]/}"; b="${b//[^0-9]/}"
    a="${a:-0}"; b="${b:-0}"
    if [ "$a" -gt "$b" ]; then return 0; fi
    if [ "$a" -lt "$b" ]; then return 1; fi
  done
  return 0
}

# find_python : first python3/python on PATH that is >= 3.10
find_python() {
  local cand v
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      v="$("$cand" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null)" || continue
      if version_ge "$v" "3.10.0"; then
        echo "$cand"
        return 0
      fi
    fi
  done
  return 1
}

# ---------- steps ----------
step_node() {
  if ! command -v node >/dev/null 2>&1; then
    fail "node" "install Node.js 18 or newer (LTS) from https://nodejs.org and run this script again"
    return
  fi
  local v
  v="$(node --version 2>/dev/null | sed 's/^v//')"
  if version_ge "$v" "18.0.0"; then
    ok "node $v"
  else
    fail "node" "found $v; install Node.js 18 or newer (LTS) from https://nodejs.org"
  fi
}

step_npm_install() {
  if [ -d "$ROOT/node_modules/electron" ]; then
    ok "npm install (already present)"
    return
  fi
  if ! command -v npm >/dev/null 2>&1; then
    fail "npm install" "npm not found; install Node.js from https://nodejs.org"
    return
  fi
  if run npm install --no-audit --no-fund; then
    ok "npm install"
  else
    fail "npm install" "re-run 'npm install --no-audit --no-fund' in $ROOT and read its errors"
  fi
}

step_claude_cli() {
  if command -v claude >/dev/null 2>&1 && claude --version >/dev/null 2>&1; then
    ok "claude code cli ($(claude --version 2>/dev/null | head -n1))"
    return
  fi
  if ! command -v npm >/dev/null 2>&1; then
    fail "claude code cli" "npm not found; install Node.js then run 'npm install -g @anthropic-ai/claude-code'"
    return
  fi
  if run npm install -g @anthropic-ai/claude-code; then
    ok "claude code cli (installed; run 'claude' once in a terminal to log in)"
  else
    fail "claude code cli" "run 'npm install -g @anthropic-ai/claude-code' (may need sudo or an npm prefix you own), then run 'claude' once to log in"
  fi
}

PYTHON=""
step_python() {
  if PYTHON="$(find_python)"; then
    ok "python ($("$PYTHON" --version 2>&1))"
  else
    fail "python" "install Python 3.10 or newer from https://www.python.org/downloads/ (Linux: apt/dnf python3 + python3-venv) and run this script again"
  fi
}

step_venv_engine() {
  if [ -z "$PYTHON" ]; then
    fail "engine venv" "skipped: Python 3.10+ not found"
    return
  fi
  if [ ! -x "$VENV_PY" ]; then
    if ! run "$PYTHON" -m venv "$VENV"; then
      fail "engine venv" "could not create $VENV; on Debian/Ubuntu install python3-venv first"
      return
    fi
  fi
  if [ "$DRY" = "1" ] && [ ! -x "$VENV_PY" ]; then
    echo "  [dry] $VENV_PIP install -e $ROOT/engine"
    ok "engine venv + pip install -e engine"
    return
  fi
  run "$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1
  if run "$VENV_PY" -m pip install -e "$ROOT/engine"; then
    ok "engine venv + pip install -e engine"
  else
    fail "engine venv" "run '$VENV_PY -m pip install -e $ROOT/engine' and read its errors"
  fi
}

step_playwright() {
  if [ ! -x "$VENV_PY" ] && [ "$DRY" != "1" ]; then
    fail "playwright chromium" "skipped: engine venv missing"
    return
  fi
  if run "$VENV_PY" -m playwright install chromium; then
    ok "playwright chromium"
  else
    fail "playwright chromium" "run '$VENV_PY -m playwright install chromium' (Linux may also need '$VENV_PY -m playwright install-deps chromium' with sudo)"
  fi
}

# O's settings file (Electron userData of app "O AI"); the installer only merges keys into it.
settings_file() {
  if [ "$OS" = "Darwin" ]; then
    echo "$HOME/Library/Application Support/O AI/o-settings.json"
  else
    echo "${XDG_CONFIG_HOME:-$HOME/.config}/O AI/o-settings.json"
  fi
}

# set_setting <key> <value> : merges one string key into the settings JSON, keeping everything else
set_setting() {
  local file py
  file="$(settings_file)"
  if [ "$DRY" = "1" ]; then
    echo "  [dry] set $1=$2 in $file"
    return 0
  fi
  py="${PYTHON:-python3}"
  "$py" - "$file" "$1" "$2" <<'EOF'
import json, os, sys
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
data = {}
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    pass
data[key] = value
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
os.replace(tmp, path)
EOF
}

# vLLM: local install on Linux with an NVIDIA GPU. Elsewhere: a remote or Docker vLLM URL is required.
VLLM_DOCKER="docker run --gpus all -p 8000:8000 vllm/vllm-openai --model meta-llama/Meta-Llama-3-8B-Instruct (on a machine with an NVIDIA GPU)"
step_vllm_url() {
  local url="$VLLM_URL_ARG"
  if [ -z "$url" ] && [ "$DRY" != "1" ] && [ -t 0 ]; then
    echo "  vLLM cannot run locally here. Start one elsewhere, e.g.: $VLLM_DOCKER"
    printf "  vLLM URL (OpenAI-compatible, e.g. http://gpu-box:8000/v1): "
    read -r url
  fi
  if [ -z "$url" ]; then
    if [ "$DRY" = "1" ]; then
      echo "  [dry] prompt for vLLM URL (or pass --vllm-url URL), then save it as vllmUrl"
      ok "vllm (remote URL)"
      return
    fi
    fail "vllm" "no vLLM URL given; start one with '$VLLM_DOCKER' and run this script again with --vllm-url http://<host>:8000/v1"
    return
  fi
  case "$url" in
    http://*|https://*) ;;
    *) fail "vllm" "'$url' is not an http(s) URL; pass e.g. --vllm-url http://gpu-box:8000/v1"; return ;;
  esac
  url="${url%/}"
  if ! set_setting vllmUrl "$url"; then
    fail "vllm" "could not write vllmUrl to $(settings_file); set Settings → Engine → vLLM URL to $url by hand"
    return
  fi
  if [ "$DRY" = "1" ]; then
    echo "  [dry] curl -fsS $url/models"
    ok "vllm (remote URL $url)"
  elif curl -fsS --max-time 10 "$url/models" >/dev/null 2>&1; then
    ok "vllm (remote URL $url saved as vllmUrl, reachable)"
  else
    fail "vllm" "saved vllmUrl=$url but $url/models did not answer; start the server ($VLLM_DOCKER) and run this script again"
  fi
}

step_vllm() {
  if [ "$OS" = "Linux" ] && command -v nvidia-smi >/dev/null 2>&1; then
    if [ ! -x "$VENV_PY" ] && [ "$DRY" != "1" ]; then
      fail "vllm" "skipped: engine venv missing"
      return
    fi
    if run "$VENV_PY" -m pip install vllm; then
      ok "vllm (local; start it with '$VENV_PY -m vllm.entrypoints.openai.api_server --model <vllmModel> --port 8000')"
    else
      fail "vllm" "run '$VENV_PY -m pip install vllm' and read its errors, or run this script with --vllm-url pointing at a remote vLLM"
    fi
    [ -n "$VLLM_URL_ARG" ] && step_vllm_url
  else
    [ "$OS" = "Linux" ] && echo "  no NVIDIA GPU (nvidia-smi) found; vLLM must run on another machine or in Docker on a GPU host"
    step_vllm_url
  fi
}

# screenpipe: published installer one-liner from https://github.com/mediar-ai/screenpipe
# If this command stops working, check that page for the current install command.
step_screenpipe() {
  if command -v screenpipe >/dev/null 2>&1; then
    ok "screenpipe (already installed)"
    return
  fi
  if [ "$DRY" = "1" ]; then
    echo "  [dry] curl -fsSL get.screenpi.pe/cli | sh"
    ok "screenpipe"
    return
  fi
  if curl -fsSL get.screenpi.pe/cli | sh; then
    ok "screenpipe (open a new terminal if 'screenpipe' is not on PATH yet)"
  else
    fail "screenpipe" "run 'curl -fsSL get.screenpi.pe/cli | sh'; if it fails, see https://github.com/mediar-ai/screenpipe for the current installer"
  fi
}

# Xvfb: Linux only (isolated display :99). macOS: headless-only.
step_xvfb() {
  if [ "$OS" != "Linux" ]; then
    ok "xvfb — isolated display: headless-only on this OS"
    return
  fi
  if command -v Xvfb >/dev/null 2>&1; then
    ok "xvfb (already installed)"
    return
  fi
  if command -v apt-get >/dev/null 2>&1; then
    if sudo_run apt-get install -y xvfb; then
      ok "xvfb"
    else
      fail "xvfb" "run 'sudo apt-get install -y xvfb'"
    fi
  elif command -v dnf >/dev/null 2>&1; then
    if sudo_run dnf install -y xorg-x11-server-Xvfb; then
      ok "xvfb"
    else
      fail "xvfb" "run 'sudo dnf install -y xorg-x11-server-Xvfb'"
    fi
  else
    fail "xvfb" "install the Xvfb package with your distro's package manager"
  fi
}

# Required engine packages: installed by 'pip install -e engine' (engine/pyproject.toml); verified by import.
# mcp = MCP tool routing, e2b = E2B containment, crewai = three-role swarm, browser_use = headless web agent.
REQUIRED_MODULES="mcp:mcp e2b:e2b crewai:crewai browser_use:browser-use"
step_required_packages() {
  if [ ! -x "$VENV_PY" ] && [ "$DRY" != "1" ]; then
    fail "engine packages" "skipped: engine venv missing"
    return
  fi
  local pair mod pkg
  for pair in $REQUIRED_MODULES; do
    mod="${pair%%:*}"; pkg="${pair#*:}"
    if [ "$DRY" = "1" ]; then
      echo "  [dry] $VENV_PY -c 'import $mod'"
      ok "$pkg"
    elif "$VENV_PY" -c "import $mod" >/dev/null 2>&1; then
      ok "$pkg"
    else
      fail "$pkg" "run '$VENV_PY -m pip install -e $ROOT/engine' and read its errors (then '$VENV_PY -c \"import $mod\"')"
    fi
  done
}

# open-interpreter: required, installed on its own. Its release pins anthropic<0.38 and psutil<6, while
# browser-use needs psutil>=7 and a newer anthropic, so pip cannot resolve both. It is installed with
# --no-deps plus its runtime dependencies unpinned; setuptools<81 keeps pkg_resources, which it imports.
OI_SPEC="open-interpreter>=0.4.3,<0.5"
OI_DEPS="astor git-python google-generativeai html2image html2text inquirer ipykernel jupyter-client litellm matplotlib nltk platformdirs pyperclip pyyaml selenium send2trash shortuuid six tiktoken tokentrim toml webdriver-manager wget yaspin setuptools<81"
step_open_interpreter() {
  if [ ! -x "$VENV_PY" ] && [ "$DRY" != "1" ]; then
    fail "open-interpreter" "skipped: engine venv missing"
    return
  fi
  local fix="run '$VENV_PY -m pip install --no-deps \"$OI_SPEC\"' and '$VENV_PY -m pip install $OI_DEPS' and read their errors"
  # shellcheck disable=SC2086
  if run "$VENV_PY" -m pip install --no-deps "$OI_SPEC" && run "$VENV_PY" -m pip install $OI_DEPS; then
    if [ "$DRY" = "1" ]; then
      echo "  [dry] $VENV_PY -c 'from interpreter import interpreter'"
      ok "open-interpreter"
    elif "$VENV_PY" -W ignore -c "from interpreter import interpreter" >/dev/null 2>&1; then
      ok "open-interpreter"
    else
      fail "open-interpreter" "installed but 'from interpreter import interpreter' fails; $fix"
    fi
  else
    fail "open-interpreter" "$fix"
  fi
}

# Docker: used for risk:"high" steps (docker run --rm --network <dockerNetwork>, default bridge). Reported, never installed.
step_docker() {
  if command -v docker >/dev/null 2>&1; then
    ok "docker (found; the engine uses it for high-risk steps when the daemon is running)"
  else
    echo "✗ docker — optional; install Docker from https://docs.docker.com/get-docker/ for disposable high-risk sandboxes, or set highRiskSandbox to e2b/local"
  fi
}

step_engine_check() {
  local bin="$VENV/bin/o-engine"
  if [ "$DRY" = "1" ]; then
    echo "  [dry] $bin check"
    ok "engine check"
    return
  fi
  if [ ! -x "$bin" ]; then
    fail "engine check" "engine not installed yet (run this script again after fixing the engine venv step)"
    return
  fi
  if "$bin" check; then
    ok "engine check"
  else
    fail "engine check" "'$bin check' reported problems above; fix them and run it again"
  fi
}

# ---------- main ----------
echo "O AI install — $OS — repo: $ROOT"
[ "$DRY" = "1" ] && echo "(dry run: commands are printed, not executed)"
echo

step_node
step_npm_install
step_claude_cli
step_python
step_venv_engine
step_playwright
step_vllm
step_screenpipe
step_xvfb
step_required_packages
step_open_interpreter
step_docker
step_engine_check

echo
echo "Set Settings → Engine → Python to: $VENV_PY"
echo "  (copy exactly: $VENV_PY)"
echo "If Claude Code was just installed, run 'claude' once in a terminal to log in (your Claude subscription powers O)."
echo

if [ "${#FAILED[@]}" -eq 0 ]; then
  echo "All steps succeeded."
  exit 0
fi

echo "Failed steps (${#FAILED[@]}):"
for s in "${FAILED[@]}"; do
  echo "  - $s"
done
exit "${#FAILED[@]}"
