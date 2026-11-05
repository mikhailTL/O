#!/usr/bin/env bash
# O AI installer — Linux and macOS.
# Idempotent: safe to run again at any time. Each step prints "✓ name" or "✗ name — what to do".
# Exit code = number of failed required steps. vLLM on macOS (or Linux without an NVIDIA GPU)
# counts as failed-but-continue; the alternative is printed. Phase 2 packages and Docker are optional
# and never count as failures.
# Set O_INSTALL_DRY=1 to print the commands instead of running them.
set -u

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

DRY="${O_INSTALL_DRY:-0}"
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

# vLLM: Linux with an NVIDIA GPU only. Elsewhere, point O at a remote vLLM.
VLLM_ALT="run vLLM on a Linux box with CUDA or in Docker 'vllm/vllm-openai' and set Settings → Engine → vLLM URL"
step_vllm() {
  if [ "$OS" = "Linux" ] && command -v nvidia-smi >/dev/null 2>&1; then
    if [ ! -x "$VENV_PY" ] && [ "$DRY" != "1" ]; then
      fail "vllm" "skipped: engine venv missing"
      return
    fi
    if run "$VENV_PY" -m pip install vllm; then
      ok "vllm"
    else
      fail "vllm" "run '$VENV_PY -m pip install vllm' and read its errors, or $VLLM_ALT"
    fi
  elif [ "$OS" = "Linux" ]; then
    fail "vllm" "no NVIDIA GPU (nvidia-smi) found on this Linux machine; $VLLM_ALT"
  else
    fail "vllm" "not supported locally on $OS; $VLLM_ALT"
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

# Phase 2 packages: MCP routing, Open Interpreter runner, E2B containment, crewAI swarm, browser-use.
# Each is optional; the engine falls back without it. One pip call per package, none fatal.
PHASE2_PKGS="mcp open-interpreter e2b crewai browser-use"
step_phase2_packages() {
  if [ ! -x "$VENV_PY" ] && [ "$DRY" != "1" ]; then
    fail "phase 2 packages" "skipped: engine venv missing"
    return
  fi
  local pkg missing=()
  for pkg in $PHASE2_PKGS; do
    if [ "$DRY" = "1" ]; then
      echo "  [dry] $VENV_PY -m pip install $pkg"
      ok "phase 2: $pkg"
    elif "$VENV_PY" -m pip install "$pkg" >/dev/null 2>&1; then
      ok "phase 2: $pkg"
    else
      echo "  ✗ phase 2: $pkg — optional; run '$VENV_PY -m pip install $pkg' later if you want it"
      missing+=("$pkg")
    fi
  done
  if [ "${#missing[@]}" -eq 0 ]; then
    ok "phase 2 packages"
  else
    ok "phase 2 packages (${#missing[@]} optional package(s) not installed: ${missing[*]})"
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
step_phase2_packages
step_docker
step_engine_check

echo
echo "Set Settings → Engine → Python to: $VENV_PY"
echo "  (copy exactly: $VENV_PY)"
echo "If Claude Code was just installed, run 'claude' once in a terminal to log in."
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
