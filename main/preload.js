'use strict';
// Bridge between the renderer windows and the main process.
// Exposed as window.o in both the pet and the panel windows.
const { contextBridge, ipcRenderer } = require('electron');

// The saved theme, read synchronously so the renderer can set <html data-theme> before first paint.
let initialTheme = 'glass';
try {
  const t = ipcRenderer.sendSync('settings:theme');
  if (['glass', 'light', 'dark'].includes(t)) initialTheme = t;
} catch (_) { /* main not ready: the renderer falls back to settings:get */ }

contextBridge.exposeInMainWorld('o', {
  initialTheme,
  // pet window
  moveBy: (dx, dy) => ipcRenderer.send('pet:moveBy', Math.round(dx) || 0, Math.round(dy) || 0),
  getBounds: () => ipcRenderer.invoke('pet:getBounds'),
  getWorkArea: () => ipcRenderer.invoke('pet:getWorkArea'),
  getCursor: () => ipcRenderer.invoke('pet:getCursor'),
  togglePanel: () => ipcRenderer.send('panel:toggle'),
  setIgnoreMouse: (ignore) => ipcRenderer.send('pet:setIgnoreMouse', !!ignore),
  fling: (vx, vy) => ipcRenderer.send('pet:fling', Number(vx) || 0, Number(vy) || 0),
  grab: () => ipcRenderer.send('pet:grab'),

  // both windows
  onAgentEvent: (cb) => {
    const handler = (_e, ev) => cb(ev);
    ipcRenderer.on('agent:event', handler);
    return () => ipcRenderer.removeListener('agent:event', handler);
  },
  sendPrompt: (text) => ipcRenderer.invoke('agent:prompt', String(text || '')),
  cancel: () => ipcRenderer.invoke('agent:cancel'),
  newSession: () => ipcRenderer.invoke('agent:newSession'),
  // chat history
  listChats: () => ipcRenderer.invoke('chats:list'),
  currentChat: () => ipcRenderer.invoke('chats:current'),
  newChat: () => ipcRenderer.invoke('chats:new'),
  openChat: (id) => ipcRenderer.invoke('chats:open', String(id)),
  renameChat: (id, title) => ipcRenderer.invoke('chats:rename', String(id), String(title || '')),
  deleteChats: (ids) => ipcRenderer.invoke('chats:delete', ids === 'all' ? 'all' : [].concat(ids).map(String)),
  respondPermission: (id, allow) => ipcRenderer.send('agent:permission', id, !!allow),
  getSettings: () => ipcRenderer.invoke('settings:get'),
  setSettings: (partial) => ipcRenderer.invoke('settings:set', partial || {}),
  checkClaude: () => ipcRenderer.invoke('claude:check'),

  // extras (panel)
  hidePanel: () => ipcRenderer.send('panel:hide'),
  turnOff: () => ipcRenderer.send('app:quit'),
  petMenu: () => ipcRenderer.send('pet:menu'),
  // permission sidebar
  onTheme: (cb) => {
    const handler = (_e, ev) => { if (ev && ev.type === 'theme') cb(ev.theme); };
    ipcRenderer.on('agent:event', handler);
    return () => ipcRenderer.removeListener('agent:event', handler);
  },
  onNotify: (cb) => {
    const handler = (_e, list) => cb(list);
    ipcRenderer.on('notify:perms', handler);
    return () => ipcRenderer.removeListener('notify:perms', handler);
  },
  notifyResize: (h) => ipcRenderer.send('notify:resize', Number(h) || 0),
  showPanelFromNotify: () => ipcRenderer.send('notify:openChat'),
  getState: () => ipcRenderer.invoke('app:state'),
  createShortcuts: () => ipcRenderer.invoke('app:shortcuts'),
  linkStatus: () => ipcRenderer.invoke('link:status'),
  connectLink: () => ipcRenderer.invoke('link:connect'),
  checkExtension: () => ipcRenderer.invoke('browser:extension'),
  installExtension: () => ipcRenderer.invoke('browser:installExtension'),
  checkUpdate: () => ipcRenderer.invoke('update:check'),
  applyUpdate: () => ipcRenderer.invoke('update:apply'),
  getUpdateState: () => ipcRenderer.invoke('update:state'),
  // action log + engine
  logTail: (n) => ipcRenderer.invoke('log:tail', Number(n) || 200),
  logMarkRead: () => ipcRenderer.invoke('log:markRead'),
  logPaths: () => ipcRenderer.invoke('log:paths'),
  logOpenFile: () => ipcRenderer.invoke('log:openFile'),
  logVerifyChain: () => ipcRenderer.invoke('log:verify'),
  onLogEntry: (cb) => {
    const handler = (_e, ev) => { if (ev && ev.type === 'log') cb(ev.entry); };
    ipcRenderer.on('agent:event', handler);
    return () => ipcRenderer.removeListener('agent:event', handler);
  },
  engineStatus: () => ipcRenderer.invoke('engine:status'),
  // GenUI (Phase 3): frames arrive as agent:event {type:'genui', frame}; the renderer validates them
  onGenui: (cb) => {
    const handler = (_e, ev) => { if (ev && ev.type === 'genui' && ev.frame) cb(ev.frame); };
    ipcRenderer.on('agent:event', handler);
    return () => ipcRenderer.removeListener('agent:event', handler);
  },
  genuiFrames: (surface) => ipcRenderer.invoke('genui:frames', String(surface || '')),
  genuiSurfaces: () => ipcRenderer.invoke('genui:surfaces'),
  genuiAction: (action, payload) => ipcRenderer.invoke('genui:action', String(action || ''), payload && typeof payload === 'object' ? payload : {}),
  checkRequirements: () => ipcRenderer.invoke('requirements:check'),
  // API proxy
  logNote: (summary, detail) => ipcRenderer.invoke('log:note', String(summary || ''), detail || {}),
  proxyStatus: () => ipcRenderer.invoke('proxy:status'),
  proxyRegenerateToken: () => ipcRenderer.invoke('proxy:regenToken'),
  platform: process.platform,
});
