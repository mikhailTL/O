// Stand-in for main/preload.js (window.o) inside the panel frame embedded by the scene system (motion/scene.js). Injected with
// Playwright's addInitScript, so it runs before the renderer's own scripts. Same surface as preload.js;
// log, GenUI, state and prompt calls go to the demo backend (real engine + real action log) through the hub.
(function () {
  if (!window.frameElement || !window.parent || !window.parent.__stage) return;
  var hub = window.parent.__stage;
  var local = function (v) { return function () { return Promise.resolve(typeof v === 'function' ? v.apply(null, arguments) : v); }; };
  var remote = function (method) { return function () { return hub.rpc(method, Array.prototype.slice.call(arguments)); }; };
  window.o = {
    // Island window (status capsule + command bar)
    getBounds: local(function () { return hub.getBounds(); }),
    getWorkArea: local(function () { return hub.getWorkArea(); }),
    getCursor: local(function () { return hub.getCursor(); }),
    togglePanel: function () { hub.togglePanel(); },
    setIgnoreMouse: function () {},
    // both windows
    onAgentEvent: function (cb) { return hub.onAgentEvent(cb); },
    sendPrompt: remote('sendPrompt'),
    cancel: remote('cancel'),
    newSession: local(undefined),
    listChats: remote('listChats'),
    currentChat: remote('currentChat'),
    newChat: remote('newChat'),
    openChat: remote('openChat'),
    renameChat: remote('renameChat'),
    deleteChats: remote('deleteChats'),
    respondPermission: function (id, allow) { hub.rpc('respondPermission', [id, allow]); hub.emit({ type: 'permission-resolved', id: id, allow: allow }); },
    getSettings: local(function () { return Object.assign({}, hub.settings); }),
    setSettings: local(function (p) { Object.assign(hub.settings, p || {}); return {}; }),
    checkClaude: local({ installed: true, version: '2.1.316', loggedIn: true }),
    // panel extras
    hidePanel: function () { hub.hidePanel(); },
    turnOff: function () {},
    petMenu: function () { hub.togglePanel(); },
    onNotify: function () { return function () {}; },
    notifyResize: function () {},
    showPanelFromNotify: function () { hub.showPanel(); },
    getState: remote('getState'),
    createShortcuts: local({ ok: true }),
    linkStatus: local({ connected: true, tabs: 3 }),
    connectLink: local({ ok: true }),
    checkExtension: local({ installed: true, browser: 'msedge', profile: 'Default' }),
    installExtension: local({ ok: true }),
    checkUpdate: local({ state: 'current', message: 'Up to date' }),
    applyUpdate: local({ state: 'current' }),
    getUpdateState: local({ state: 'idle' }),
    // action log + engine
    logTail: remote('logTail'),
    logMarkRead: remote('logMarkRead'),
    logPaths: remote('logPaths'),
    logOpenFile: remote('logOpenFile'),
    logVerifyChain: remote('logVerifyChain'),
    onLogEntry: function (cb) { return hub.onAgentEvent(function (ev) { if (ev && ev.type === 'log') cb(ev.entry); }); },
    engineStatus: remote('engineStatus'),
    // GenUI
    onGenui: function (cb) { return hub.onAgentEvent(function (ev) { if (ev && ev.type === 'genui' && ev.frame) cb(ev.frame); }); },
    genuiFrames: remote('genuiFrames'),
    genuiSurfaces: remote('genuiSurfaces'),
    genuiAction: remote('genuiAction'),
    checkRequirements: remote('checkRequirements'),
    // API proxy
    logNote: remote('logNote'),
    proxyStatus: remote('proxyStatus'),
    proxyRegenerateToken: local({ ok: true }),
    platform: 'win32',
  };
  // Mouse moves inside a frame are reported to the stage in stage coordinates (the hub's cursorAt; the current scene stage ignores them).
  document.addEventListener('mousemove', function (e) {
    var r = window.frameElement.getBoundingClientRect(); // viewport px; e.clientX is frame px
    hub.cursorAt(e.clientX + r.left / hub.K, e.clientY + r.top / hub.K);
  }, true);
})();
