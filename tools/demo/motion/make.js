'use strict';
// Make one film: start the real backend, do the take (films.js), render every scene file (render.js), join
// them with the concat demuxer, write <out>.mp4 and <out>.json. `stills` renders PNGs at each file's busiest
// moments instead (for a look before the full render).
const fs = require('fs');
const os = require('os');
const path = require('path');
const { startBackend, ROOT } = require('../run');
const { openStudio, renderScene, assemble, probe, genuiStore } = require('./render');
const { films, CU_TREE } = require('./films');

/** What render.js's makeMain reads from a backend, with nothing running behind it. */
function standIn() {
  const { EventEmitter } = require('events');
  const ev = new EventEmitter();
  return Object.assign(ev, {
    actionLog: { tail: () => [], markRead: () => ({ ok: true }), paths: () => ({}), verifyChain: () => ({ ok: true, count: 0 }), record: () => {} },
    engine: { port: 0, url: '' }, health: { vllm: false, playwright: true },
    proxy: { status: () => ({ enabled: false }) },
    stop: async () => {},
  });
}

async function makeFilm(name, { out, stills = false, stillsDir = null, audio = null, keepScenes = false, scenes = null, log = console.log } = {}) {
  if (!films[name]) throw new Error(`unknown film ${name} (have: ${Object.keys(films).join(', ')})`);
  const work = fs.mkdtempSync(path.join(os.tmpdir(), `o-film-${name}-`));
  // an animated film (films[name].standalone) needs no take: the panel frames talk to a quiet stand-in
  const backend = films[name].standalone ? standIn() : await startBackend({ dataDir: path.join(work, 'data'), cuTree: CU_TREE });
  const genui = genuiStore();
  const state = { busy: false, logVisible: null, genuiVisible: null, genui };
  backend.on('event', (ev) => { if (ev.type === 'genui') genui.push(ev.frame); });
  let studio = null;
  try {
    log(`[${name}] take…`);
    const film = await films[name](backend, state);
    studio = await openStudio(backend, state, genui);
    const results = [];
    for (const f of film.files) {
      if (scenes && !scenes.includes(f.id)) continue;
      log(`[${name}] scene ${f.id} (${f.film.map((s) => s.shot).join(' → ')})`);
      if (stills) {
        const r = await renderScene(studio, { id: f.id, film: f.film, edges: f.edges || film.edges, cues: film.cues, stills: f.stills || [Math.round(1200)], stillsDir: stillsDir || path.join(work, 'stills'), log });
        results.push(r);
      } else {
        results.push(await renderScene(studio, { id: f.id, film: f.film, edges: f.edges || film.edges, cues: film.cues, out: path.join(work, `${f.id}.mp4`), log }));
      }
    }
    const facts = film.facts ? film.facts() : {};
    if (stills) return { name, title: film.title, scenes: results, facts };
    fs.mkdirSync(path.dirname(out), { recursive: true });
    const info = assemble(results.map((r) => r.file), out, { audio });
    const timeline = []; let t = 0;
    for (const r of results) { timeline.push({ scene: r.id, start: +t.toFixed(2), seconds: +r.durationS.toFixed(2), bytes: r.bytes }); t += r.durationS; }
    if (keepScenes) { const dir = out.replace(/\.mp4$/, '-scenes'); fs.mkdirSync(dir, { recursive: true }); for (const r of results) fs.copyFileSync(r.file, path.join(dir, path.basename(r.file))); }
    const sidecar = { name, title: film.title, file: path.relative(ROOT, out), bytes: fs.statSync(out).size, ...info, fps: 30, audio: !!audio, timeline, facts, renderedAt: new Date().toISOString() };
    fs.writeFileSync(out.replace(/\.mp4$/, '.json'), JSON.stringify(sidecar, null, 2));
    log(`[${name}] ${sidecar.file} ${info.durationS.toFixed(1)} s ${(sidecar.bytes / 1e6).toFixed(1)} MB ${info.width}x${info.height}`);
    return sidecar;
  } finally {
    if (studio) await studio.close().catch(() => {});
    await backend.stop().catch(() => {});
    if (!stills) fs.rmSync(work, { recursive: true, force: true });
  }
}

module.exports = { makeFilm, probe };
