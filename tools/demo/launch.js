#!/usr/bin/env node
'use strict';
// Renders docs/videos/o-launch.mp4: the launch film (90–120 s) as scene files from tools/demo/motion/films.js,
// joined with ffmpeg's concat demuxer (-c copy). No music or narration is bundled; --audio muxes a track.
//
//   node tools/demo/launch.js                   # docs/videos/o-launch.mp4 + o-launch.json
//   --out <file>                                # output path
//   --audio <file>                              # mux this audio track (video copied, audio aac)
//   --stills <dir>                              # PNGs at each scene's busiest moments instead of a render
//   --keep-scenes                               # also keep the per-scene mp4s next to the output
const path = require('path');
const { makeFilm } = require('./motion/make');
const { ROOT } = require('./run');

async function main() {
  const argv = process.argv.slice(2);
  let out = path.join(ROOT, 'docs', 'videos', 'o-launch.mp4');
  let audio = null, stills = null, keepScenes = false;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--out') out = path.resolve(argv[++i]);
    else if (argv[i] === '--audio') audio = path.resolve(argv[++i]);
    else if (argv[i] === '--stills') stills = path.resolve(argv[++i]);
    else if (argv[i] === '--keep-scenes') keepScenes = true;
  }
  const r = await makeFilm('launch', { out, audio, stills: !!stills, stillsDir: stills, keepScenes });
  if (stills) { for (const s of r.scenes) console.log(s.stills.join('\n')); return; }
  for (const s of r.timeline) console.log(`  ${String(s.start).padStart(7)} s  ${s.scene} (${s.seconds} s)`);
}

main().catch((e) => { console.error(e); process.exit(1); });
