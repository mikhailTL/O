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
[ -f .o-installed ] || { bash install/install.sh $VLLM && touch .o-installed; } || echo "Installer reported problems above — O will still start; paste any ✗ lines to Claude."
[ -d node_modules/electron ] || npm install || { echo "npm install failed"; read -n1 -p "Press any key to close"; exit 1; }
echo "Starting O…"
npm start
