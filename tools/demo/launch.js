#!/usr/bin/env node
'use strict';
// Assembles docs/videos/o-launch.mp4 from the recorded demos: title card, demo segments (their lower-third
// captions are part of the recordings), a differentiator card (claims taken from README.md) and an end card.
// Cards are rendered from cards.html with Playwright to PNG, turned into clips with ffmpeg, and everything is
// concatenated losslessly-ish (same codec settings as record.js). No audio unless --voiceover is given.
//
//   node tools/demo/launch.js                       # docs/videos/d1..d4.mp4 -> docs/videos/o-launch.mp4
//   --segments d1,d3,d4                             # pick / order the demo segments (default: every dN.mp4)
//   --out <file>                                    # output path
//   --voiceover <dir>                               # mux <dir>/title.wav, d1.wav … d4.wav, why.wav, end.wav
//                                                   # (each starts with its segment; missing files are silence)

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');
const { chromium } = require(process.env.O_DEMO_PLAYWRIGHT || 'playwright');
const { probe, hasFfmpeg, W, H, SCALE, FPS } = require('./record');
const { ROOT } = require('./run');

const HERE = __dirname;
const CARD_S = { title: 3.5, why: 9, end: 4 };

async function renderCards(dir) {
  const browser = await chromium.launch(process.env.O_DEMO_CHROME ? { executablePath: process.env.O_DEMO_CHROME } : {});
  const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: SCALE });
  await page.goto('file://' + path.join(HERE, 'cards.html'));
  const out = {};
  for (const id of ['title', 'why', 'end']) {
    await page.evaluate((id) => { document.querySelectorAll('.card').forEach((c) => c.classList.toggle('on', c.id === id)); }, id);
    await page.waitForTimeout(150);
    out[id] = path.join(dir, `${id}.png`);
    await page.screenshot({ path: out[id] });
  }
  await browser.close();
  return out;
}

function ff(args) { execFileSync('ffmpeg', ['-v', 'error', '-y', ...args], { stdio: ['ignore', 'inherit', 'inherit'] }); }

/** A still card as a clip with a fade in and out, same encoding as the demo segments. */
function cardClip(png, seconds, out) {
  ff(['-loop', '1', '-framerate', String(FPS), '-t', String(seconds), '-i', png,
    '-vf', `scale=${W * SCALE}:${H * SCALE},fade=t=in:st=0:d=0.5,fade=t=out:st=${(seconds - 0.6).toFixed(2)}:d=0.6,format=yuv420p`,
    '-c:v', 'libx264', '-preset', 'medium', '-crf', '21', '-r', String(FPS), '-an', out]);
}

/** Re-encode a demo segment so every piece shares codec, size, fps and timebase; a short fade on each end. */
function segmentClip(src, out) {
  const d = probe(src).durationS;
  ff(['-i', src, '-vf', `scale=${W * SCALE}:${H * SCALE},fps=${FPS},fade=t=in:st=0:d=0.4,fade=t=out:st=${Math.max(0, d - 0.5).toFixed(2)}:d=0.5,format=yuv420p`,
    '-c:v', 'libx264', '-preset', 'medium', '-crf', '21', '-an', out]);
}

async function main() {
  if (!hasFfmpeg()) throw new Error('ffmpeg is required to assemble the launch video (the per-demo recordings are still usable as is)');
  const argv = process.argv.slice(2);
  const videos = path.join(ROOT, 'docs', 'videos');
  let out = path.join(videos, 'o-launch.mp4');
  let segments = null, voiceover = null;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--out') out = path.resolve(argv[++i]);
    else if (argv[i] === '--segments') segments = argv[++i].split(',').map((s) => s.trim()).filter(Boolean);
    else if (argv[i] === '--voiceover') voiceover = path.resolve(argv[++i]);
  }
  if (!segments) segments = fs.readdirSync(videos).filter((f) => /^d\d+\.mp4$/.test(f)).sort().map((f) => f.replace(/\.mp4$/, ''));
  if (!segments.length) throw new Error('no demo recordings in docs/videos; run `npm run demo:record` first');
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'o-launch-'));
  console.log('cards…');
  const cards = await renderCards(work);
  const pieces = []; // {name, file, seconds}
  const add = (name, file) => pieces.push({ name, file, seconds: probe(file).durationS });
  cardClip(cards.title, CARD_S.title, path.join(work, 'c-title.mp4')); add('title', path.join(work, 'c-title.mp4'));
  for (const id of segments) {
    const src = path.join(videos, `${id}.mp4`);
    if (!fs.existsSync(src)) throw new Error(`missing ${src}; run \`node tools/demo/record.js ${id}\``);
    console.log(`segment ${id}…`);
    const dst = path.join(work, `s-${id}.mp4`);
    segmentClip(src, dst); add(id, dst);
  }
  cardClip(cards.why, CARD_S.why, path.join(work, 'c-why.mp4')); add('why', path.join(work, 'c-why.mp4'));
  cardClip(cards.end, CARD_S.end, path.join(work, 'c-end.mp4')); add('end', path.join(work, 'c-end.mp4'));

  const list = path.join(work, 'list.txt');
  fs.writeFileSync(list, pieces.map((p) => `file '${p.file.replace(/'/g, "'\\''")}'`).join('\n') + '\n');
  fs.mkdirSync(path.dirname(out), { recursive: true });
  console.log('concat…');
  const silent = path.join(work, 'silent.mp4');
  ff(['-f', 'concat', '-safe', '0', '-i', list, '-c', 'copy', '-movflags', '+faststart', silent]);

  if (voiceover) {
    // one wav per piece, started at that piece's offset; absent files are skipped (silence)
    const inputs = [], delays = [];
    let t = 0, n = 0;
    for (const p of pieces) {
      const wav = path.join(voiceover, `${p.name}.wav`);
      if (fs.existsSync(wav)) { inputs.push('-i', wav); delays.push(`[${n + 1}:a]adelay=${Math.round(t * 1000)}|${Math.round(t * 1000)}[a${n}]`); n++; }
      t += p.seconds;
    }
    if (!n) { fs.copyFileSync(silent, out); console.log('no wavs found in', voiceover); }
    else {
      const mix = delays.join(';') + ';' + delays.map((_, i) => `[a${i}]`).join('') + `amix=inputs=${n}:normalize=0[a]`;
      ff(['-i', silent, ...inputs, '-filter_complex', mix, '-map', '0:v', '-map', '[a]', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '160k', '-shortest', '-movflags', '+faststart', out]);
    }
  } else {
    fs.copyFileSync(silent, out);
  }
  const info = probe(out);
  const timeline = []; let t = 0;
  for (const p of pieces) { timeline.push({ name: p.name, start: +t.toFixed(2), seconds: +p.seconds.toFixed(2) }); t += p.seconds; }
  fs.writeFileSync(out.replace(/\.mp4$/, '.json'), JSON.stringify({ file: path.relative(ROOT, out), bytes: fs.statSync(out).size, ...info, timeline, audio: !!voiceover, builtAt: new Date().toISOString() }, null, 2));
  fs.rmSync(work, { recursive: true, force: true });
  console.log(`${path.relative(ROOT, out)}  ${info.durationS.toFixed(1)} s  ${(fs.statSync(out).size / 1e6).toFixed(1)} MB  ${info.width}x${info.height}`);
  for (const s of timeline) console.log(`  ${String(s.start).padStart(7)} s  ${s.name} (${s.seconds} s)`);
}

main().catch((e) => { console.error(e); process.exit(1); });
