'use strict';
// Renders assets/o-avatar.svg to the app's icon files with the pre-installed Playwright Chromium
// (no extra npm deps): o-icon.png (256), o-base.png (64), tray.png (44) and o.ico (256/128/64/32/16, PNG-compressed entries).
//   node assets/build-icons.js [--chrome /path/to/chrome] [--playwright /path/to/node_modules/playwright]
const fs = require('fs');
const path = require('path');

const args = process.argv.slice(2);
const opt = (k, d) => { const i = args.indexOf(k); return i !== -1 ? args[i + 1] : d; };
const PW = opt('--playwright', '/opt/npm-tools/node_modules/playwright');
const CHROME = opt('--chrome', process.env.CHROME_PATH || '/usr/bin/google-chrome');
const DIR = __dirname;
const SVG = fs.readFileSync(path.join(DIR, 'o-avatar.svg'), 'utf8');

async function renderAll() {
  const { chromium } = require(PW);
  const browser = await chromium.launch({ executablePath: fs.existsSync(CHROME) ? CHROME : undefined, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const out = {};
  for (const size of [256, 128, 64, 44, 32, 16]) {
    await page.setViewportSize({ width: size, height: size });
    await page.setContent(`<!doctype html><html><body style="margin:0;background:transparent">${SVG.replace(/width="256" height="256"/, `width="${size}" height="${size}"`)}</body></html>`);
    out[size] = await page.screenshot({ omitBackground: true, type: 'png', clip: { x: 0, y: 0, width: size, height: size } });
  }
  await browser.close();
  return out;
}

// ICO container: 6-byte header, 16-byte directory entries, then the PNG blobs (valid on Vista+).
function ico(pngs) {
  const head = Buffer.alloc(6);
  head.writeUInt16LE(0, 0); head.writeUInt16LE(1, 2); head.writeUInt16LE(pngs.length, 4);
  const dir = [];
  let offset = 6 + 16 * pngs.length;
  for (const { size, buf } of pngs) {
    const e = Buffer.alloc(16);
    e.writeUInt8(size >= 256 ? 0 : size, 0); e.writeUInt8(size >= 256 ? 0 : size, 1);
    e.writeUInt8(0, 2); e.writeUInt8(0, 3); e.writeUInt16LE(1, 4); e.writeUInt16LE(32, 6);
    e.writeUInt32LE(buf.length, 8); e.writeUInt32LE(offset, 12);
    offset += buf.length; dir.push(e);
  }
  return Buffer.concat([head, ...dir, ...pngs.map((p) => p.buf)]);
}

(async () => {
  const png = await renderAll();
  fs.writeFileSync(path.join(DIR, 'o-icon.png'), png[256]);
  fs.writeFileSync(path.join(DIR, 'o-base.png'), png[64]);
  fs.writeFileSync(path.join(DIR, 'tray.png'), png[44]);
  fs.writeFileSync(path.join(DIR, 'o.ico'), ico([256, 128, 64, 32, 16].map((size) => ({ size, buf: png[size] }))));
  for (const f of ['o-icon.png', 'o-base.png', 'tray.png', 'o.ico']) console.log(f, fs.statSync(path.join(DIR, f)).size, 'bytes');
})().catch((e) => { console.error(e); process.exit(1); });
