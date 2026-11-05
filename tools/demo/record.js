#!/usr/bin/env node
'use strict';
// Renders the four demo films (d1–d4, ≤ 35 s each) with the scene system in tools/demo/motion/: real engine jobs,
// cache replays, action log + chain verification, API proxy and computer-use runs, cut as isolated UI fragments
// and typography on black glass. Output: docs/videos/<id>.mp4 (3840×2160, h264 30 fps, no audio) + <id>.json.
//
//   node tools/demo/record.js            # all demos
//   node tools/demo/record.js d1 d3      # some
//   --out <dir>      where the videos go (default docs/videos)
//   --stills <dir>   render PNGs at each scene's busiest moments instead of video (check before a full render)
//
// O_DEMO_PLAYWRIGHT can point at another Playwright install; O_DEMO_CHROME at a Chrome/Chromium binary.
const path = require('path');
const { makeFilm } = require('./motion/make');
const { ROOT } = require('./run');

async function main() {
  const argv = process.argv.slice(2);
  const opt = { outDir: path.join(ROOT, 'docs', 'videos'), stills: null };
  const ids = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--out') opt.outDir = path.resolve(argv[++i]);
    else if (argv[i] === '--stills') opt.stills = path.resolve(argv[++i]);
    else ids.push(argv[i]);
  }
  const todo = ids.length ? ids : ['d1', 'd2', 'd3', 'd4'];
  const results = [];
  for (const id of todo) {
    if (!/^d[1-4]$/.test(id)) throw new Error(`unknown demo ${id} (have: d1 d2 d3 d4)`);
    results.push(await makeFilm(id, { out: path.join(opt.outDir, `${id}.mp4`), stills: !!opt.stills, stillsDir: opt.stills }));
  }
  if (opt.stills) { for (const r of results) for (const s of r.scenes) console.log(s.stills.join('\n')); return; }
  console.log('\n' + results.map((r) => `${r.file}\t${r.durationS.toFixed(1)} s\t${(r.bytes / 1e6).toFixed(1)} MB`).join('\n'));
}

if (require.main === module) main().catch((e) => { console.error(e); process.exit(1); });
