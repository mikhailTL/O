#!/usr/bin/env node
'use strict';
// Renders a film from tools/demo/motion/films.js as scene files joined with ffmpeg's concat demuxer (-c copy).
// No music or narration is bundled; --audio muxes a track.
//
//   node tools/demo/launch.js                   # docs/videos/o-launch.mp4 + o-launch.json (the engine film)
//   --film film                                 # the animated consumer film -> docs/videos/o-film.mp4 + .json
//   --out <file>                                # output path
//   --audio <file>                              # mux this audio track (video copied, audio aac)
//   --stills <dir>                              # PNGs at each scene's busiest moments instead of a render
//   --keep-scenes                               # also keep the per-scene mp4s next to the output
//   --scenes 01-open,02-tasks                   # render only these scene files (for test clips)
//   --film film4                                # the v4 film with the cel FX layer -> docs/videos/o-film4.mp4 + .json
const path = require('path');
const { makeFilm } = require('./motion/make');
const { ROOT } = require('./run');

async function main() {
  const argv = process.argv.slice(2);
  let out = null, film = 'launch';
  let audio = null, stills = null, keepScenes = false, scenes = null;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--film') film = argv[++i];
    else if (argv[i] === '--out') out = path.resolve(argv[++i]);
    else if (argv[i] === '--audio') audio = path.resolve(argv[++i]);
    else if (argv[i] === '--stills') stills = path.resolve(argv[++i]);
    else if (argv[i] === '--keep-scenes') keepScenes = true;
    else if (argv[i] === '--scenes') scenes = argv[++i].split(',');
  }
  if (!out) out = path.join(ROOT, 'docs', 'videos', film === 'launch' ? 'o-launch.mp4' : `o-${film}.mp4`);
  const r = await makeFilm(film, { out, audio, stills: !!stills, stillsDir: stills, keepScenes, scenes });
  if (stills) { for (const s of r.scenes) console.log(s.stills.join('\n')); return; }
  for (const s of r.timeline) console.log(`  ${String(s.start).padStart(7)} s  ${s.scene} (${s.seconds} s)`);
}

main().catch((e) => { console.error(e); process.exit(1); });
