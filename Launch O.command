#!/bin/bash
# One-click install + launch for O on macOS. Safe to run again: steps already done are skipped.
cd "$(dirname "$0")" || exit 1
echo "Installing O (first run takes a few minutes)…"
VLLM="--vllm-url http://127.0.0.1:11434/v1"
[ -f .o-installed ] || { bash install/install.sh $VLLM && touch .o-installed; } || echo "Installer reported problems above — O will still start; paste any ✗ lines to Claude."
[ -d node_modules ] || npm install || { echo "npm install failed"; read -n1 -p "Press any key to close"; exit 1; }
echo "Starting O…"
npm start
