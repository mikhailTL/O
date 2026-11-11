#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)" || exit 1

# Cloud-synced folders (Google Drive, iCloud, Dropbox, OneDrive) move files mid-run and break Electron.
SYNCED=""
case "$DIR" in
  */Library/CloudStorage/*|*/Mobile\ Documents/*|*/Dropbox/*|*/OneDrive*) SYNCED=1 ;;
esac
[ -d "$(dirname "$DIR")/.tmp.driveupload" ] && SYNCED=1
if [ -n "$SYNCED" ]; then
  DEST="$HOME/O"
  [ -e "$DEST" ] && DEST="$HOME/O-$(date +%Y%m%d%H%M%S)"
  echo "This folder is cloud-synced; moving O to $DEST…"
  mv "$DIR" "$DEST" || { echo "Move failed — drag the folder to your home folder and run Launch O.command again."; read -n1 -p "Press any key to close"; exit 1; }
  rm -rf "$DEST/node_modules"
  exec bash "$DEST/Launch O.command"
fi

cd "$DIR" || exit 1

if [ "$(uname)" = "Darwin" ] && [ -d node_modules/electron ] && \
   [ ! -e "node_modules/electron/dist/Electron.app/Contents/Frameworks/Electron Framework.framework" ]; then
  echo "Electron is incomplete; reinstalling it…"
  rm -rf node_modules/electron
fi

echo "Installing O (first run takes a few minutes)…"
VLLM="--vllm-url http://127.0.0.1:11434/v1"
# The marker is written after the first run whatever the installer reported: a soft step (vLLM not running,
# screenpipe) must not re-run the whole installer on every launch; the refresh checks below pick up anything
# that is still missing (app packages, engine packages, Chromium).
if [ ! -f .o-installed ]; then
  bash install/install.sh $VLLM || echo "Installer reported problems above — O will still start; paste any ✗ lines to Claude."
  touch .o-installed
fi

# Self-update: the latest main before every start (Windows' launcher does the same), then whatever the pull
# changed gets installed: app packages when package.json moved, engine packages when pyproject did, Playwright's
# Chromium when it is missing. A stale node_modules was why O fell back to `npx @playwright/mcp@latest` per task.
if [ -d .git ] && command -v git >/dev/null 2>&1; then
  git pull -q --ff-only origin main 2>/dev/null || echo "(could not pull the latest O; starting the version on disk)"
fi
if [ ! -d node_modules/electron ] || [ package.json -nt node_modules/.package-lock.json ]; then
  echo "Installing app packages…"
  npm install --no-audit --no-fund || { echo "npm install failed"; read -n1 -p "Press any key to close"; exit 1; }
fi
VENV_PY="engine/.venv/bin/python"
if [ -x "$VENV_PY" ]; then
  if [ engine/pyproject.toml -nt engine/.venv/.o-installed ]; then
    echo "Updating engine packages…"
    "$VENV_PY" -m pip install -q -e engine && touch engine/.venv/.o-installed
  fi
  "$VENV_PY" -c "from playwright.sync_api import sync_playwright
import sys
with sync_playwright() as p:
    sys.exit(0 if __import__('os').path.exists(p.chromium.executable_path) else 1)" 2>/dev/null || "$VENV_PY" -m playwright install chromium >/dev/null 2>&1 || true
fi
echo "Starting O…"
npm start
