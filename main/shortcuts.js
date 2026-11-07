'use strict';
// Easy ways to open O: Desktop + Start menu shortcuts (Windows) and "start when I log in".
//
// O usually runs from source (`npm start`), so the shortcuts point at the Electron binary in
// node_modules with the app folder as its argument. They carry O's AppUserModelID, which
// Windows also needs to show O's notifications.

const fs = require('fs');
const path = require('path');

const APP_ID = 'ai.o.agent';
const NAME = 'O AI';

function launchTarget(app) {
  const appDir = path.join(__dirname, '..');
  // Packaged builds start themselves; from source, run Electron with the app folder.
  return app.isPackaged
    ? { target: process.execPath, args: '', argv: [], cwd: path.dirname(process.execPath) }
    : { target: process.execPath, args: `"${appDir}"`, argv: [appDir], cwd: appDir };
}

function shortcutPaths(app) {
  const startMenu = path.join(app.getPath('appData'), 'Microsoft', 'Windows', 'Start Menu', 'Programs');
  return { desktop: path.join(app.getPath('desktop'), `${NAME}.lnk`), startMenu: path.join(startMenu, `${NAME}.lnk`) };
}

/**
 * Create or refresh the Desktop and Start menu shortcuts (Windows only).
 * @param {{ desktop?: boolean }} opts  desktop:false only refreshes the Start menu one
 * @returns {{ ok: boolean, created: string[], error?: string }}
 */
function createShortcuts(app, shell, { desktop = true } = {}) {
  if (process.platform !== 'win32') return { ok: false, created: [], error: 'Shortcuts are made on Windows only. Use `npm start` or Start O.' };
  const t = launchTarget(app);
  const icon = path.join(__dirname, '..', 'assets', 'o.ico');
  const link = {
    target: t.target,
    args: t.args,
    cwd: t.cwd,
    description: 'O AI, a self-directed desktop agent',
    icon: fs.existsSync(icon) ? icon : t.target,
    iconIndex: 0,
    appUserModelId: APP_ID,
  };
  const p = shortcutPaths(app);
  const created = [];
  const targets = desktop ? [p.desktop, p.startMenu] : [p.startMenu];
  for (const file of targets) {
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      const op = fs.existsSync(file) ? 'replace' : 'create';
      if (shell.writeShortcutLink(file, op, link)) created.push(file);
    } catch (_) { /* best effort */ }
  }
  return { ok: created.length > 0, created };
}

/** Is a shortcut pointing at this copy of O? (So we refresh it if O was moved.) */
function shortcutsCurrent(app, shell) {
  if (process.platform !== 'win32') return true;
  try {
    const s = shell.readShortcutLink(shortcutPaths(app).startMenu);
    return s.target === launchTarget(app).target && s.args === launchTarget(app).args;
  } catch { return false; }
}

function setOpenAtLogin(app, on) {
  const t = launchTarget(app);
  try {
    app.setLoginItemSettings({ openAtLogin: !!on, path: t.target, args: t.argv, name: NAME });
    return true;
  } catch { return false; }
}

function getOpenAtLogin(app) {
  const t = launchTarget(app);
  try { return !!app.getLoginItemSettings({ path: t.target, args: t.argv }).openAtLogin; } catch { return false; }
}

module.exports = { APP_ID, createShortcuts, shortcutsCurrent, setOpenAtLogin, getOpenAtLogin, launchTarget };
