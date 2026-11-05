'use strict';
// O AI settings: a small JSON file in the app's userData folder.
const fs = require('fs');
const path = require('path');
const os = require('os');

const DEFAULTS = Object.freeze({
  theme: 'glass', // 'glass' (translucent near-black, default) | 'light' | 'dark' (opaque fallbacks)
  permissionMode: 'log', // 'log' (record everything, never ask) | 'ask' (blocking card, 60 s) | 'auto'
  browserHeadless: true, // invisible by default so no window ever pops up over a game
  cwd: os.homedir(),
  claudePath: 'claude',
  model: '',
  browser: '', // '' = auto-detect installed Edge/Chrome/Brave/Chromium (Edge first) | 'chrome' | 'msedge' | 'chromium' | 'firefox'
  browserPath: '', // optional explicit browser executable; overrides `browser`
  browserMode: 'link', // 'link' = every tab of your own Edge/Chrome via O Link | 'mine' = Playwright Extension (tab group) | 'separate' = O's own invisible browser
  extensionToken: '', // PLAYWRIGHT_MCP_EXTENSION_TOKEN from the Playwright Extension (skips the connect dialog)
  browserProfile: '', // optional Chrome profile dir name for extension mode, e.g. "Profile 1"
  maxParallelTasks: 3, // 1..8 tasks run at once (each its own Claude Code process); more wait in a queue
  quiet: true, // never take keyboard focus on its own (safe while gaming)
  screenAccess: true, // let O take screenshots of your screen (see_screen tool)
  autoUpdateCheck: true, // check GitHub for a new version at startup and every few hours
  updateRepo: 'slithr-in4/O',
  updateBranch: 'main',
  updatePath: '', // folder inside the repo that holds O ('' = the repo root)
  updateToken: '', // optional read-only GitHub token, needed only if the repo is private
  openAtLogin: false, // start O when you log in
  shortcutsMade: false, // internal: Desktop/Start menu shortcuts were created once
  // engine (Python, engine/)
  enginePython: '', // '' = auto (python3 / python / py -3)
  vllmUrl: 'http://127.0.0.1:8000/v1', // local OpenAI-compatible vLLM server
  vllmModel: 'meta-llama/Meta-Llama-3-8B-Instruct',
  screenpipeDb: '', // '' = screenpipe's default SQLite path
  sandboxRoot: path.join(os.homedir(), 'o-sandbox'), // engine commands run under this tree
  maxRemediationAttempts: 3, // 1..10
  logUnreadBadge: true, // badge + "O did N things while you were away" when the panel is closed
  // API proxy (main/proxy.js): localhost, OpenAI-compatible, bearer token
  proxyEnabled: true,
  proxyPort: 8787,
  proxyToken: '', // generated on first start, never logged
  proxyConcurrency: 4, // 1..16 parallel claude -p children
  proxyMode: 'code', // 'code' (persona stripped, code-only template) | 'chat'
  // computer use (engine/o_engine/cu)
  cuCandidates: 1, // best-of-N plans, each in its own isolated display (Linux)
  cuMaxHeals: 3, // bounded self-heal attempts per procedure
  vllmVisionModel: '', // local VLM for grounding/judging; '' = none (Claude Code fallback)
  // computer use, wave 2 (reactive milestone loop and its gates; engine env O_CU_*)
  cuStepsPerTurn: 5, // 1..20 steps the planner may emit per observation
  cuMaxTurns: 20, // 1..200 plan/observe turns per task before giving up
  cuGroundConfidence: 0.3, // 0..1; a grounding answer below this is discarded and the fallback runs
  cuStepCandidates: 1, // 1..8 candidate plans per step, judged on the tree (no extra displays)
  cuFeasibilityGate: true, // read-only probes + one model question before planning
  cuZoomGrounding: true, // ground on the full screenshot, then again on a crop around the answer
  cuComparativeRanking: true, // best-of-N ranks all candidates in one model call
  cuInterfaceBuilder: true, // turn a passing procedure into a per-app Python interface (api.py)
  // Phase 2 tooling core
  mcpServers: '{}', // JSON, Claude Code's mcpServers shape; merged into o-mcp.json and the engine's router
  highRiskSandbox: 'docker', // 'e2b' | 'docker' | 'local' for risk:"high" steps
  e2bApiKey: '', // local secret; never logged
  dockerImage: 'python:3.12-slim',
  dockerNetwork: 'bridge', // 'none' blocks installs inside the container
  swarmEnabled: true,
  // Phase 3 GenUI
  genuiEnabled: true,
  genuiSandbox: true, // allow sandboxed iframe previews
  // Ambient sensing (plan J): everything off until the person turns it on, one sensor at a time
  ambientEnabled: false, // master switch: presence polling, away mode, the brief
  sensorWifi: false, // Wi-Fi CSI room motion (room-level presence only)
  sensorSonar: false, // 18-22 kHz chirps through the speakers, echoes through the mics
  sensorCamera: false, // webcam face presence (is someone at the screen)
  sensorHeartRate: false, // webcam rPPG heart rate, ephemeral, never logged
  absentAfterMin: 5, // 1..120 minutes without presence before O treats you as away
  briefSpeak: false, // read the welcome-back brief aloud
  settingsVersion: 5,
});

const MODES = ['log', 'ask', 'auto'];
const THEMES = ['glass', 'light', 'dark'];

class Settings {
  constructor(userDataDir) {
    this.file = path.join(userDataDir, 'o-settings.json');
    this.data = { ...DEFAULTS };
    try {
      const raw = JSON.parse(fs.readFileSync(this.file, 'utf8'));
      this.data = { ...DEFAULTS, ...sanitize(raw) };
      // v2: O works in your own browser tab by default (was: its own invisible browser).
      // v3: O Link (all your tabs, O's own extension) is the default way to use your browser.
      if (!(raw.settingsVersion >= 3)) {
        this.data.browserMode = 'link';
        this.data.settingsVersion = 3;
      }
      // v4: 'log' is THE default of O (nothing asks, everything is recorded). Users still on
      // the old 'ask' default move to 'log'; 'auto' was an explicit choice and is kept.
      if (!(raw.settingsVersion >= 4)) {
        if (this.data.permissionMode === 'ask') this.data.permissionMode = 'log';
        this.data.settingsVersion = 4;
      }
      // O moved from the claude-team repo to its own repo: follow it.
      if (this.data.updateRepo === 'samuelsortland-cmyk/claude-team') {
        Object.assign(this.data, { updateRepo: DEFAULTS.updateRepo, updateBranch: DEFAULTS.updateBranch, updatePath: DEFAULTS.updatePath });
      }
    } catch (_) { /* first run or broken file: keep defaults */ }
  }

  get() {
    if (!this.data.proxyToken) {
      // v5: the API proxy refuses to listen without a token; make one once and persist it.
      this.set({ proxyToken: require('crypto').randomBytes(24).toString('hex'), settingsVersion: 5 });
    }
    return { ...this.data };
  }

  set(partial) {
    this.data = { ...this.data, ...sanitize(partial || {}) };
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      fs.writeFileSync(this.file, JSON.stringify(this.data, null, 2));
    } catch (err) {
      console.error('[o] could not save settings:', err.message);
    }
    return this.get();
  }
}

function sanitize(obj) {
  const out = {};
  if (THEMES.includes(obj.theme)) out.theme = obj.theme;
  if (MODES.includes(obj.permissionMode)) out.permissionMode = obj.permissionMode;
  if (typeof obj.browserHeadless === 'boolean') out.browserHeadless = obj.browserHeadless;
  if (typeof obj.cwd === 'string' && obj.cwd.trim()) out.cwd = obj.cwd.trim();
  if (typeof obj.claudePath === 'string' && obj.claudePath.trim()) out.claudePath = obj.claudePath.trim();
  if (typeof obj.model === 'string') out.model = obj.model.trim();
  if (['', 'chrome', 'msedge', 'chromium', 'firefox'].includes(obj.browser)) out.browser = obj.browser;
  if (typeof obj.browserPath === 'string') out.browserPath = obj.browserPath.trim();
  if (['link', 'mine', 'separate'].includes(obj.browserMode)) out.browserMode = obj.browserMode;
  if (typeof obj.extensionToken === 'string') out.extensionToken = obj.extensionToken.trim();
  if (typeof obj.browserProfile === 'string') out.browserProfile = obj.browserProfile.trim();
  if (typeof obj.quiet === 'boolean') out.quiet = obj.quiet;
  if (obj.maxParallelTasks !== undefined && obj.maxParallelTasks !== null && obj.maxParallelTasks !== '') {
    const n = parseInt(obj.maxParallelTasks, 10);
    if (Number.isFinite(n)) out.maxParallelTasks = Math.max(1, Math.min(8, n));
  }
  if (typeof obj.openAtLogin === 'boolean') out.openAtLogin = obj.openAtLogin;
  if (typeof obj.shortcutsMade === 'boolean') out.shortcutsMade = obj.shortcutsMade;
  if (typeof obj.screenAccess === 'boolean') out.screenAccess = obj.screenAccess;
  if (typeof obj.autoUpdateCheck === 'boolean') out.autoUpdateCheck = obj.autoUpdateCheck;
  if (typeof obj.updateRepo === 'string') out.updateRepo = obj.updateRepo.trim();
  if (typeof obj.updateBranch === 'string') out.updateBranch = obj.updateBranch.trim();
  if (typeof obj.updatePath === 'string') out.updatePath = obj.updatePath.trim().replace(/^\/+|\/+$/g, '');
  if (typeof obj.updateToken === 'string') out.updateToken = obj.updateToken.trim();
  if (typeof obj.enginePython === 'string') out.enginePython = obj.enginePython.trim();
  if (typeof obj.vllmUrl === 'string' && obj.vllmUrl.trim()) out.vllmUrl = obj.vllmUrl.trim().replace(/\/+$/, '');
  if (typeof obj.vllmModel === 'string' && obj.vllmModel.trim()) out.vllmModel = obj.vllmModel.trim();
  if (typeof obj.screenpipeDb === 'string') out.screenpipeDb = obj.screenpipeDb.trim();
  if (typeof obj.sandboxRoot === 'string' && obj.sandboxRoot.trim()) out.sandboxRoot = obj.sandboxRoot.trim();
  if (obj.maxRemediationAttempts !== undefined) {
    const n = parseInt(obj.maxRemediationAttempts, 10);
    if (Number.isFinite(n)) out.maxRemediationAttempts = Math.max(1, Math.min(10, n));
  }
  if (typeof obj.logUnreadBadge === 'boolean') out.logUnreadBadge = obj.logUnreadBadge;
  if (typeof obj.proxyEnabled === 'boolean') out.proxyEnabled = obj.proxyEnabled;
  if (obj.proxyPort !== undefined) { const n = parseInt(obj.proxyPort, 10); if (Number.isFinite(n)) out.proxyPort = Math.max(1024, Math.min(65535, n)); }
  if (typeof obj.proxyToken === 'string') out.proxyToken = obj.proxyToken.trim();
  if (obj.proxyConcurrency !== undefined) { const n = parseInt(obj.proxyConcurrency, 10); if (Number.isFinite(n)) out.proxyConcurrency = Math.max(1, Math.min(16, n)); }
  if (obj.proxyMode === 'code' || obj.proxyMode === 'chat') out.proxyMode = obj.proxyMode;
  if (obj.cuCandidates !== undefined) { const n = parseInt(obj.cuCandidates, 10); if (Number.isFinite(n)) out.cuCandidates = Math.max(1, Math.min(8, n)); }
  if (obj.cuMaxHeals !== undefined) { const n = parseInt(obj.cuMaxHeals, 10); if (Number.isFinite(n)) out.cuMaxHeals = Math.max(1, Math.min(10, n)); }
  if (typeof obj.vllmVisionModel === 'string') out.vllmVisionModel = obj.vllmVisionModel.trim();
  if (obj.cuStepsPerTurn !== undefined) { const n = parseInt(obj.cuStepsPerTurn, 10); if (Number.isFinite(n)) out.cuStepsPerTurn = Math.max(1, Math.min(20, n)); }
  if (obj.cuMaxTurns !== undefined) { const n = parseInt(obj.cuMaxTurns, 10); if (Number.isFinite(n)) out.cuMaxTurns = Math.max(1, Math.min(200, n)); }
  if (obj.cuGroundConfidence !== undefined) { const n = parseFloat(obj.cuGroundConfidence); if (Number.isFinite(n)) out.cuGroundConfidence = Math.max(0, Math.min(1, Math.round(n * 100) / 100)); }
  if (obj.cuStepCandidates !== undefined) { const n = parseInt(obj.cuStepCandidates, 10); if (Number.isFinite(n)) out.cuStepCandidates = Math.max(1, Math.min(8, n)); }
  if (typeof obj.cuFeasibilityGate === 'boolean') out.cuFeasibilityGate = obj.cuFeasibilityGate;
  if (typeof obj.cuZoomGrounding === 'boolean') out.cuZoomGrounding = obj.cuZoomGrounding;
  if (typeof obj.cuComparativeRanking === 'boolean') out.cuComparativeRanking = obj.cuComparativeRanking;
  if (typeof obj.cuInterfaceBuilder === 'boolean') out.cuInterfaceBuilder = obj.cuInterfaceBuilder;
  if (typeof obj.mcpServers === 'string') { try { const j = JSON.parse(obj.mcpServers || '{}'); if (j && typeof j === 'object' && !Array.isArray(j)) out.mcpServers = JSON.stringify(j); } catch (_) { /* keep the previous value */ } }
  if (['e2b', 'docker', 'local'].includes(obj.highRiskSandbox)) out.highRiskSandbox = obj.highRiskSandbox;
  if (typeof obj.e2bApiKey === 'string') out.e2bApiKey = obj.e2bApiKey.trim();
  if (typeof obj.dockerImage === 'string' && obj.dockerImage.trim()) out.dockerImage = obj.dockerImage.trim();
  if (typeof obj.dockerNetwork === 'string' && /^[a-z0-9_.-]+$/i.test(obj.dockerNetwork.trim())) out.dockerNetwork = obj.dockerNetwork.trim();
  if (typeof obj.swarmEnabled === 'boolean') out.swarmEnabled = obj.swarmEnabled;
  if (typeof obj.genuiEnabled === 'boolean') out.genuiEnabled = obj.genuiEnabled;
  if (typeof obj.genuiSandbox === 'boolean') out.genuiSandbox = obj.genuiSandbox;
  for (const k of ['ambientEnabled', 'sensorWifi', 'sensorSonar', 'sensorCamera', 'sensorHeartRate', 'briefSpeak']) if (typeof obj[k] === 'boolean') out[k] = obj[k];
  if (obj.absentAfterMin !== undefined && obj.absentAfterMin !== null && obj.absentAfterMin !== '') { const n = parseInt(obj.absentAfterMin, 10); if (Number.isFinite(n)) out.absentAfterMin = Math.max(1, Math.min(120, n)); }
  if (Number.isInteger(obj.settingsVersion) && obj.settingsVersion >= 1) out.settingsVersion = obj.settingsVersion;
  return out;
}

module.exports = { Settings, DEFAULTS };
