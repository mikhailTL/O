'use strict';
// Cel FX for the launch film v4 (films.film4): 2D anime-style energy drawn procedurally and deterministically.
//
//  - flame(): a shader flame (one shared WebGL2 context renders into each instance's own 2D canvas). The field
//    is a teardrop / ring / polyline body eroded by domain-warped noise, cut into flat bands with hard
//    anti-aliased edges (deep -> mid -> pale -> white core, colours from film4.css --fx-*), a rim-coloured copy offset by a few px behind it
//    (the chromatic rim), edge grain, and bloom from blurred low-res copies.
//  - ink(): a full-frame 2D canvas for strokes: speed lines and smears, impact rings, bolts, bursts, sparks.
//  - hud(): viewfinder corner brackets that snap onto a target and track it; grid(): the faint dot grid
//    with pulses from the orb; flash(): flash frames.
// Every cel is re-drawn on a step clock ("animated on twos/threes": 15 or 10 fps inside the 30 fps film), and
// every random choice is seeded by (seed, step), so a render is repeatable frame for frame.
(function () {
  var S = window.__scene, L = S.lib, el = L.el, esc = L.esc;

  // the palette lives in film4.css (:root --fx-*); C.cyan is the chromatic rim colour whatever its hue
  var C = { deep: '#1a2fb8', mid: '#3a7bff', pale: '#a8c8ff', core: '#ffffff', cyan: '#8fe6ff' };
  (function readPalette() {
    try {
      var cs = getComputedStyle(document.documentElement);
      [['deep', '--fx-deep'], ['mid', '--fx-mid'], ['pale', '--fx-pale'], ['core', '--fx-core'], ['cyan', '--fx-rim']].forEach(function (kv) {
        var v = cs.getPropertyValue(kv[1]).trim(); if (/^#[0-9a-f]{6}$/i.test(v)) C[kv[0]] = v;
      });
    } catch (_) {}
  })();
  function rgba(hex, a) { var n = parseInt(hex.slice(1), 16); return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + a + ')'; }
  var TWOS = 1000 / 15, THREES = 100;
  var DPR = window.devicePixelRatio || 1;

  function clamp(p) { return p < 0 ? 0 : p > 1 ? 1 : p; }
  function eo(p) { p = clamp(p); return 1 - Math.pow(1 - p, 3); }
  function eio(p) { p = clamp(p); return p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2; }
  function rng(seed) { seed = (seed >>> 0) || 1; return function () { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; }; }
  function stepOf(ms, stepMs) { return Math.floor((ms + 0.5) / stepMs); }
  function quant(ms, stepMs) { return stepOf(ms, stepMs) * stepMs; }
  function put(node, x, y, w, h) {
    node.style.position = 'absolute'; node.style.left = x + 'px'; node.style.top = y + 'px';
    if (w != null) node.style.width = w + 'px';
    if (h != null) node.style.height = h + 'px';
    return node;
  }

  // ------------------------------------------------------------------ shared WebGL flame renderer
  var VS = '#version 300 es\nin vec2 a;void main(){gl_Position=vec4(a,0.,1.);}';
  var FS = [
    '#version 300 es',
    'precision highp float;',
    'uniform vec2 uRes; uniform float uDpr; uniform float uT; uniform float uSeed; uniform int uMode;',
    'uniform float uI; uniform vec2 uBase; uniform vec2 uSize; uniform float uRingR;',
    'uniform vec2 uPts[48]; uniform int uN; uniform float uReveal; uniform float uThick; uniform float uHead;',
    'uniform vec2 uRim; uniform vec3 uClip; uniform float uTurb; uniform float uRise; uniform float uFreq; uniform float uStep;',
    'uniform vec3 cDeep; uniform vec3 cMid; uniform vec3 cPale; uniform vec3 cCore; uniform vec3 cCyan;',
    'out vec4 o;',
    // simplex noise (Ashima / Stefan Gustavson, MIT)
    'vec3 perm(vec3 x){return mod(((x*34.0)+1.0)*x,289.0);}',
    'float sn(vec2 v){const vec4 C=vec4(.211324865405187,.366025403784439,-.577350269189626,.024390243902439);',
    ' vec2 i=floor(v+dot(v,C.yy));vec2 x0=v-i+dot(i,C.xx);vec2 i1=(x0.x>x0.y)?vec2(1.,0.):vec2(0.,1.);',
    ' vec4 x12=x0.xyxy+C.xxzz;x12.xy-=i1;i=mod(i,289.);vec3 p=perm(perm(i.y+vec3(0.,i1.y,1.))+i.x+vec3(0.,i1.x,1.));',
    ' vec3 m=max(.5-vec3(dot(x0,x0),dot(x12.xy,x12.xy),dot(x12.zw,x12.zw)),0.);m=m*m;m=m*m;',
    ' vec3 x=2.*fract(p*C.www)-1.;vec3 h=abs(x)-.5;vec3 ox=floor(x+.5);vec3 a0=x-ox;',
    ' m*=1.79284291400159-.85373472095314*(a0*a0+h*h);vec3 g;g.x=a0.x*x0.x+h.x*x0.y;g.yz=a0.yz*x12.xz+h.yz*x12.yw;',
    ' return 130.*dot(m,g);}',
    'float fbm2(vec2 p){return .5+.5*(.65*sn(p)+.35*sn(p*2.03+7.1));}',
    'float fbm4(vec2 p){float a=.5,s=0.;for(int i=0;i<3;i++){s+=a*sn(p);p=p*2.07+vec2(3.1,1.7);a*=.42;}return .5+.5*s/.798;}',
    'float hash(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}',
    'float ybase(float x){',
    ' float y=uPts[0].y;',
    ' for(int i=1;i<48;i++){ if(i>=uN) break; vec2 a=uPts[i-1], b=uPts[i]; if(x>=a.x && x<=b.x){ y=mix(a.y,b.y,(x-a.x)/max(b.x-a.x,1e-3)); } }',
    ' if(x>uPts[uN-1].x) y=uPts[uN-1].y;',
    ' return y;}',
    'float field(vec2 p){',
    ' vec2 q; vec2 nq; float v; float hgt;',
    ' if(uMode==0){',
    '  q=vec2((p.x-uBase.x)/uSize.x,(uBase.y-p.y)/uSize.y);',
    '  q.x-=.28*sin(uT*2.3+q.y*2.6+uSeed)*max(q.y,0.)*max(q.y,0.)+.1*sin(uT*3.7+q.y*5.+uSeed*2.)*max(q.y,0.);',
    '  float w=mix(1.,.16,clamp(q.y,0.,1.));',
    '  float hx=q.x/w;',
    '  v=q.y>0.?1.-hx*hx-pow(q.y/1.18,2.):1.-hx*hx-pow(q.y*3.2,2.);',
    '  hgt=clamp(q.y,0.,1.5); nq=vec2(p.x-uBase.x,uBase.y-p.y)/uSize.y;',
    ' } else if(uMode==2){',
    '  vec2 d=p-uBase; float r=length(d); vec2 dir=d/max(r,1e-3);',
    '  float h=(r-uRingR)/uSize.y;',
    '  v=h>0.?1.-pow(h/1.15,2.):1.-pow(h*uSize.y/max(uThick,.5),2.);',
    '  q=dir*(uRingR/uSize.x*.55)+dir*(h*.9);',
    '  q=vec2(q.x,q.y); hgt=clamp(h,0.,1.5);',
    '  vec2 uv=q*uFreq-dir*uT*uRise*.5;',
    '  vec2 wp=vec2(fbm2(uv*1.1+uSeed),fbm2(uv*1.1+vec2(5.2,1.3)+uSeed))-.5;',
    '  float n=fbm4(uv*1.6+wp*1.7);',
    '  return v*uI-(n-.36)*uTurb*(.25+1.1*hgt);',
    ' } else {',
    '  float yb=ybase(clamp(p.x,uPts[0].x,uReveal));',
    '  float head=exp(-pow((p.x-uReveal)/(uSize.x*1.2),2.))*uHead;',
    '  float tail=smoothstep(uPts[0].x-uThick,uPts[0].x+uSize.x*1.4,p.x);',
    '  float lick=.22+1.15*pow(fbm2(vec2(p.x/(uSize.x*1.3)+uSeed*3.1,uT*1.4)),1.6);',
    '  float hh=uSize.y*(lick+head)*mix(.45,1.,tail);',
    '  float dy=yb-p.y;',
    '  float dx=max(max(uPts[0].x-p.x,p.x-uReveal),0.);',
    '  v=dy>0.?1.-dy/hh:1.-pow(dy/max(uThick,.5),2.);',
    '  v-=pow(dx/max(uThick,.5),2.);',
    '  q=vec2(p.x/uSize.x,dy/hh); hgt=clamp(q.y,0.,1.5); nq=vec2(p.x,dy)/uSize.y;',
    ' }',
    ' vec2 uv=vec2(nq.x*uFreq,nq.y*uFreq*.8-uT*uRise);',
    ' vec2 wp=vec2(fbm2(uv*.9+uSeed),fbm2(uv*.9+vec2(5.2,1.3)+uSeed))-.5;',
    ' float n=fbm4(uv*1.3+wp*2.4+vec2(0.,-uT*uRise*.5));',
    ' return v*uI*.8-(n-.38)*uTurb*(.3+1.25*hgt);',
    '}',
    'void main(){',
    ' vec2 p=vec2(gl_FragCoord.x,uRes.y-gl_FragCoord.y)/uDpr;',
    ' float g=(hash(floor(gl_FragCoord.xy/3.)+uStep*17.31)-.5)*.009;',
    ' float f=field(p)+g;',
    ' float fo=field(p-uRim)+g;',
    ' float aa=max(fwidth(f)*.8,1e-4); float ao=max(fwidth(fo)*.8,1e-4);',
    ' float b0=smoothstep(-aa,aa,f), b1=smoothstep(.15-aa,.15+aa,f), b2=smoothstep(.36-aa,.36+aa,f), b3=smoothstep(.58-aa,.58+aa,f);',
    ' float cy=smoothstep(-ao,ao,fo)*(1.-b0);',
    ' vec3 col=mix(mix(mix(cDeep,cMid,b1),cPale,b2),cCore,b3);',
    ' col*=1.+(hash(gl_FragCoord.xy+uStep*3.7)-.5)*.05;',
    ' float a=b0+cy; vec3 c=col*b0+cCyan*cy;',
    ' if(uClip.z>0.){ float k=smoothstep(uClip.z+.8,uClip.z-.8,length(p-uClip.xy)); a*=k; c*=k; }',
    ' o=vec4(c,a);',
    '}'].join('\n');

  var GL = null;
  function hex3(h) { var n = parseInt(h.slice(1), 16); return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255]; }
  function gl() {
    if (GL) return GL;
    var cv = document.createElement('canvas'); cv.width = 64; cv.height = 64;
    var g = cv.getContext('webgl2', { premultipliedAlpha: true, alpha: true, antialias: false, preserveDrawingBuffer: true });
    if (!g) throw new Error('celfx: webgl2 unavailable');
    function sh(type, src) { var s = g.createShader(type); g.shaderSource(s, src); g.compileShader(s); if (!g.getShaderParameter(s, g.COMPILE_STATUS)) throw new Error('celfx shader: ' + g.getShaderInfoLog(s)); return s; }
    var pr = g.createProgram(); g.attachShader(pr, sh(g.VERTEX_SHADER, VS)); g.attachShader(pr, sh(g.FRAGMENT_SHADER, FS)); g.linkProgram(pr);
    if (!g.getProgramParameter(pr, g.LINK_STATUS)) throw new Error('celfx link: ' + g.getProgramInfoLog(pr));
    g.useProgram(pr);
    var buf = g.createBuffer(); g.bindBuffer(g.ARRAY_BUFFER, buf); g.bufferData(g.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), g.STATIC_DRAW);
    var loc = g.getAttribLocation(pr, 'a'); g.enableVertexAttribArray(loc); g.vertexAttribPointer(loc, 2, g.FLOAT, false, 0, 0);
    var U = {};
    ['uRes', 'uDpr', 'uT', 'uSeed', 'uMode', 'uI', 'uBase', 'uSize', 'uRingR', 'uPts', 'uN', 'uReveal', 'uThick', 'uHead', 'uRim', 'uClip', 'uTurb', 'uRise', 'uFreq', 'uStep', 'cDeep', 'cMid', 'cPale', 'cCore', 'cCyan'].forEach(function (n) { U[n] = g.getUniformLocation(pr, n); });
    g.uniform3fv(U.cDeep, hex3(C.deep)); g.uniform3fv(U.cMid, hex3(C.mid)); g.uniform3fv(U.cPale, hex3(C.pale)); g.uniform3fv(U.cCore, hex3(C.core)); g.uniform3fv(U.cCyan, hex3(C.cyan));
    g.disable(g.BLEND);
    GL = { cv: cv, g: g, U: U, w: 64, h: 64 };
    return GL;
  }
  /** Render one flame field into a w×h (device px) region of the shared canvas and copy it into ctx. */
  function renderFlame(ctx, w, h, u) {
    var R = gl(), g = R.g, U = R.U;
    if (w > R.w || h > R.h) { R.w = Math.max(R.w, w); R.h = Math.max(R.h, h); R.cv.width = R.w; R.cv.height = R.h; }
    g.viewport(0, 0, w, h);
    g.clearColor(0, 0, 0, 0); g.clear(g.COLOR_BUFFER_BIT);
    g.uniform2f(U.uRes, w, h); g.uniform1f(U.uDpr, DPR);
    g.uniform1f(U.uT, u.t); g.uniform1f(U.uSeed, u.seed || 0); g.uniform1i(U.uMode, u.mode || 0); g.uniform1f(U.uI, u.I);
    g.uniform2f(U.uBase, u.base ? u.base[0] : 0, u.base ? u.base[1] : 0); g.uniform2f(U.uSize, u.size[0], u.size[1]);
    g.uniform1f(U.uRingR, u.ringR || 0); g.uniform1f(U.uThick, u.thick || 4); g.uniform1f(U.uHead, u.head || 0);
    var pts = u.pts || [[0, 0], [1, 0]], arr = new Float32Array(96);
    for (var i = 0; i < Math.min(48, pts.length); i++) { arr[i * 2] = pts[i][0]; arr[i * 2 + 1] = pts[i][1]; }
    g.uniform2fv(U.uPts, arr); g.uniform1i(U.uN, Math.max(2, Math.min(48, pts.length)));
    g.uniform1f(U.uReveal, u.reveal == null ? 1e6 : u.reveal);
    g.uniform2f(U.uRim, u.rim ? u.rim[0] : 3, u.rim ? u.rim[1] : 2);
    g.uniform3f(U.uClip, u.clip ? u.clip[0] : 0, u.clip ? u.clip[1] : 0, u.clip ? u.clip[2] : 0);
    g.uniform1f(U.uTurb, u.turb == null ? 1.25 : u.turb); g.uniform1f(U.uRise, u.rise == null ? 1.6 : u.rise); g.uniform1f(U.uFreq, u.freq == null ? 2.4 : u.freq);
    g.uniform1f(U.uStep, u.step || 0);
    g.drawArrays(g.TRIANGLE_STRIP, 0, 4);
    ctx.clearRect(0, 0, w, h);
    ctx.drawImage(R.cv, 0, R.h - h, w, h, 0, 0, w, h);
  }

  // ------------------------------------------------------------------ bloom: low-res copies, CSS-blurred, screened
  function bloom(parent, x, y, w, h, opts) {
    opts = opts || {};
    var layers = (opts.layers || [{ blur: 6, alpha: 0.85, scale: 0.5 }, { blur: 26, alpha: 0.7, scale: 0.25 }]).map(function (b) {
      var c = document.createElement('canvas');
      c.width = Math.max(2, Math.round(w * b.scale)); c.height = Math.max(2, Math.round(h * b.scale));
      put(c, x, y, w, h); c.className = 'cfx-bloom';
      c.style.filter = 'blur(' + b.blur + 'px)'; c.style.opacity = String(b.alpha);
      parent.appendChild(c);
      return { c: c, ctx: c.getContext('2d'), b: b };
    });
    return {
      layers: layers,
      /** copy src (a canvas covering dx,dy..+dw,dh in the same css space as the bloom box) into each layer */
      from: function (src, dx, dy, dw, dh) {
        layers.forEach(function (L) {
          var k = L.b.scale;
          L.ctx.clearRect(0, 0, L.c.width, L.c.height);
          if (src) L.ctx.drawImage(src, (dx - x) * k, (dy - y) * k, dw * k, dh * k);
        });
      },
      gain: function (g) { layers.forEach(function (L) { L.c.style.opacity = String(L.b.alpha * g); }); },
    };
  }

  // ------------------------------------------------------------------ flame instance
  /**
   * flame(parent, tl, {x, y, w, h, step, seed, pad, bloom, params(tq) -> uniforms|null, z})
   * x,y,w,h: the canvas box in parent's css px; params() gets shot ms snapped to the step and returns the
   * shader uniforms in canvas-local css px (or null for nothing this step).
   */
  function flame(parent, tl, o) {
    var stepMs = o.step || TWOS, pad = o.pad == null ? Math.round(Math.max(o.w, o.h) * 0.35) : o.pad;
    var cv = document.createElement('canvas'); cv.width = Math.round(o.w * DPR); cv.height = Math.round(o.h * DPR);
    put(cv, o.x, o.y, o.w, o.h); cv.className = 'cfx-flame';
    var bl = o.bloom === false ? null : bloom(parent, o.x - pad, o.y - pad, o.w + 2 * pad, o.h + 2 * pad, o.bloom);
    parent.appendChild(cv);
    var ctx = cv.getContext('2d');
    var last = null, drawn = false;
    tl.fn(function (ms) {
      var k = stepOf(ms, stepMs), tq = k * stepMs;
      var u = o.params(tq, k);
      if (u && u.I > 0) {
        if (u.t == null) u.t = tq / 1000;
        if (u.seed == null) u.seed = o.seed || 0;
        u.step = Math.round(u.t * 30);
      }
      // a params() that holds t on a coarser clock (on threes) keeps the same key, so the cel holds
      var key = u && u.I > 0 ? JSON.stringify(u) : 'off';
      if (key === last) return;
      last = key;
      if (key === 'off') {
        if (drawn) { ctx.clearRect(0, 0, cv.width, cv.height); if (bl) bl.from(null); drawn = false; }
        return;
      }
      renderFlame(ctx, cv.width, cv.height, u);
      drawn = true;
      if (bl) { bl.from(cv, o.x, o.y, o.w, o.h); if (u.bloom != null) bl.gain(u.bloom); }
    });
    return { canvas: cv, bloom: bl };
  }

  // ------------------------------------------------------------------ cel drawing primitives (2D)
  function celStroke(ctx, pts, w, o) {
    o = o || {};
    var rim = o.rim || [3, 2], taper = o.taper;
    function pass(color, k, dx, dy) {
      ctx.strokeStyle = color; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      if (!taper) {
        ctx.lineWidth = w * k; ctx.beginPath(); ctx.moveTo(pts[0][0] + dx, pts[0][1] + dy);
        for (var i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0] + dx, pts[i][1] + dy);
        ctx.stroke(); return;
      }
      for (var j = 1; j < pts.length; j++) {
        var tw = taper(j / (pts.length - 1));
        if (tw <= 0) continue;
        ctx.lineWidth = Math.max(0.6, w * k * tw); ctx.beginPath();
        ctx.moveTo(pts[j - 1][0] + dx, pts[j - 1][1] + dy); ctx.lineTo(pts[j][0] + dx, pts[j][1] + dy); ctx.stroke();
      }
    }
    if (o.cyan !== false) pass(C.cyan, 1, rim[0], rim[1]);
    pass(C.deep, 1, 0, 0);
    pass(C.mid, 0.74, 0, 0);
    if (o.pale !== false) pass(C.pale, 0.48, 0, 0);
    pass(C.core, o.coreK || 0.26, 0, 0);
  }
  /** Fill a shape in bands: path(ctx) traced around (cx,cy); inner bands are the same path scaled toward the centre. */
  function celFill(ctx, cx, cy, path, o) {
    o = o || {};
    var rim = o.rim || [3, 2];
    var bands = o.bands || [[C.deep, 1], [C.mid, 0.78], [C.pale, 0.55], [C.core, 0.34]];
    if (o.cyan !== false) { ctx.save(); ctx.translate(rim[0], rim[1]); ctx.fillStyle = C.cyan; ctx.beginPath(); path(ctx); ctx.fill(); ctx.restore(); }
    bands.forEach(function (b) {
      ctx.save(); ctx.translate(cx, cy); ctx.scale(b[1], b[1]); ctx.translate(-cx, -cy);
      ctx.fillStyle = b[0]; ctx.beginPath(); path(ctx); ctx.fill(); ctx.restore();
    });
  }
  function starPath(cx, cy, L, w, rot) {
    return function (ctx) {
      for (var i = 0; i < 8; i++) {
        var a = rot + i * Math.PI / 4, r = i % 2 === 0 ? L : w;
        var x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.closePath();
    };
  }
  function spark(ctx, x, y, L, rot) { celFill(ctx, x, y, starPath(x, y, L, L * 0.2, rot || 0), { rim: [2, 1.5], bands: [[C.mid, 1], [C.core, 0.5]] }); }
  /** A tongue: a teardrop from (x,y) pointing along angle a, length len, half-width hw, with a curl. */
  function tonguePath(x, y, a, len, hw, curl) {
    return function (ctx) {
      var ca = Math.cos(a), sa = Math.sin(a), nx = -sa, ny = ca;
      var tx = x + ca * len + nx * curl * len, ty = y + sa * len + ny * curl * len;
      ctx.moveTo(x + nx * hw, y + ny * hw);
      ctx.quadraticCurveTo(x + ca * len * 0.55 + nx * hw * 1.1, y + sa * len * 0.55 + ny * hw * 1.1, tx, ty);
      ctx.quadraticCurveTo(x + ca * len * 0.5 - nx * hw * 0.9 + nx * curl * len * 0.4, y + sa * len * 0.5 - ny * hw * 0.9 + ny * curl * len * 0.4, x - nx * hw, y - ny * hw);
      ctx.arc(x, y, hw, a + Math.PI / 2, a - Math.PI / 2, false);
      ctx.closePath();
    };
  }

  // ------------------------------------------------------------------ ink layer: strokes on twos
  /** A full-frame 2D canvas in sec (on top unless o.under), with its own bloom. ink.add(fn(ctx, tq, k, R)). */
  function ink(sec, tl, o) {
    o = o || {};
    var W = 1920, H = 1080, stepMs = o.step || TWOS;
    var holder = put(el('div', 'cfx-ink'), 0, 0, W, H); holder.style.pointerEvents = 'none';
    var cv = document.createElement('canvas'); cv.width = W * DPR; cv.height = H * DPR; put(cv, 0, 0, W, H);
    var bl = bloom(holder, 0, 0, W, H, o.bloom || { layers: [{ blur: 5, alpha: 0.9, scale: 0.5 }, { blur: 22, alpha: 0.75, scale: 0.25 }] });
    holder.appendChild(cv);
    if (o.under) sec.insertBefore(holder, sec.firstChild); else sec.appendChild(holder);
    var ctx = cv.getContext('2d'), items = [], last = -1, dirty = false;
    var api = {
      holder: holder,
      add: function (fn) { items.push(fn); return api; },
    };
    tl.fn(function (ms) {
      var k = stepOf(ms, stepMs);
      if (k === last) return;
      last = k;
      var tq = k * stepMs;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      if (dirty) ctx.clearRect(0, 0, cv.width, cv.height);
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      var any = false;
      for (var i = 0; i < items.length; i++) { if (items[i](ctx, tq, k, rng((i + 1) * 7919 + k * 104729 + (o.seed || 0))) !== false) any = true; }
      dirty = any;
      bl.from(any ? cv : null, 0, 0, W, H);
    });
    return api;
  }

  // ---- ink items. Each returns a fn(ctx, tq, k, R) -> false when idle.
  function pt(p) { return typeof p === 'function' ? p() : p; }

  /** An energy streak (comet + smear + speed lines) from -> to between t0 and t0+dur, then a small hit. */
  function streak(o) {
    var dur = o.dur || 200, w = o.w || 16, arc = o.arc == null ? 0.18 : o.arc, hitSteps = o.hitSteps == null ? 4 : o.hitSteps;
    return function (ctx, tq, k, R) {
      var s = (tq - o.t0) / dur;
      var a = pt(o.from), b = pt(o.to);
      if (!a || !b || s < 0) return false;
      var dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy) || 1, ux = dx / len, uy = dy / len, nx = -uy, ny = ux;
      var via = o.via ? o.via(a, b) : null;
      var at = via ? function (p) { var e = eio(p), m = 1 - e; return [m * m * m * a[0] + 3 * m * m * e * via[0][0] + 3 * m * e * e * via[1][0] + e * e * e * b[0], m * m * m * a[1] + 3 * m * m * e * via[0][1] + 3 * m * e * e * via[1][1] + e * e * e * b[1]]; }
        : function (p) { var e = eio(p); var off = Math.sin(Math.PI * e) * arc * len; return [a[0] + dx * e + nx * off, a[1] + dy * e + ny * off]; };
      if (s <= 1) {
        var head = at(s), tailP = Math.max(0, s - 0.55), tail = at(tailP);
        // smear body: tapered polyline from tail to head
        var pts = []; for (var i = 0; i <= 10; i++) pts.push(at(tailP + (s - tailP) * i / 10));
        celStroke(ctx, pts, w * 1.25, { taper: function (p) { return 0.12 + 0.88 * p * p; }, rim: [3, 3] });
        celFill(ctx, head[0], head[1], function (c) { c.arc(head[0], head[1], w * 0.95, 0, Math.PI * 2); }, { rim: [3, 3] });
        // speed lines along the travel direction
        var hb = at(Math.max(0, s - 0.08)); if (via && Math.hypot(head[0] - hb[0], head[1] - hb[1]) > 1) { var dl = Math.hypot(head[0] - hb[0], head[1] - hb[1]); ux = (head[0] - hb[0]) / dl; uy = (head[1] - hb[1]) / dl; nx = -uy; ny = ux; }
        var hx = head[0], hy = head[1], tl0 = Math.hypot(head[0] - tail[0], head[1] - tail[1]) + 80;
        ctx.lineCap = 'round';
        for (var j = 0; j < 9; j++) {
          var off = (R() - 0.5) * w * 7, back = 30 + R() * tl0, l = 40 + R() * tl0 * 0.7;
          var sx = hx - ux * back + nx * off, sy = hy - uy * back + ny * off;
          ctx.strokeStyle = R() < 0.35 ? C.mid : 'rgba(255,255,255,.92)'; ctx.lineWidth = 1.5 + R() * 2.5;
          ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(sx - ux * l, sy - uy * l); ctx.stroke();
        }
        return true;
      }
      var hk = Math.floor((tq - o.t0 - dur) / TWOS);
      if (hk >= hitSteps) return false;
      // the hit: a star that pops then shrinks, a few radial lines
      var L0 = w * (2.6 - hk * 0.55);
      if (L0 > 0) spark(ctx, b[0], b[1], L0, hk * 0.4 + 0.2);
      ctx.lineCap = 'round';
      for (var m = 0; m < 7; m++) {
        var ang = R() * Math.PI * 2, r0 = w * (1.4 + hk * 1.2), r1 = r0 + w * (1.6 + R() * 1.8) * (1 - hk / hitSteps);
        ctx.strokeStyle = m % 3 === 0 ? C.mid : '#fff'; ctx.lineWidth = 2 + R() * 2;
        ctx.beginPath(); ctx.moveTo(b[0] + Math.cos(ang) * r0, b[1] + Math.sin(ang) * r0); ctx.lineTo(b[0] + Math.cos(ang) * r1, b[1] + Math.sin(ang) * r1); ctx.stroke();
      }
      return true;
    };
  }

  /** Impact: flash star + spikes on the first step, then a banded ring that expands and thins. */
  function impact(o) {
    var r = o.r || 120, steps = o.steps || 7;
    return function (ctx, tq, k, R) {
      if (tq < o.t0) return false;
      var p = pt(o.at); if (!p) return false;
      var s = Math.floor((tq - o.t0) / TWOS + 0.001);
      if (s >= steps) return false;
      var x = p[0], y = p[1];
      if (s === 0) {
        celFill(ctx, x, y, function (c) { c.arc(x, y, r * 0.32, 0, Math.PI * 2); }, { rim: [4, 3] });
        for (var i = 0; i < 12; i++) {
          var a = i / 12 * Math.PI * 2 + R() * 0.3, len = r * (0.7 + R() * 0.6);
          celFill(ctx, x, y, tonguePath(x + Math.cos(a) * r * 0.25, y + Math.sin(a) * r * 0.25, a, len, r * 0.035, 0), { rim: [3, 2], bands: [[C.mid, 1], [C.core, 0.5]] });
        }
        return true;
      }
      // a broken flame-stroke ring: tapered arc segments with gaps, tongues licking outward, stepped bands
      var rr = r * (0.45 + 0.24 * s), lw = r * 0.2 * Math.pow(1 - s / steps, 1.3) + 1.5;
      var segs = 7 + Math.floor(R() * 3), a0 = R() * Math.PI * 2;
      for (var q = 0; q < segs; q++) {
        var span = (Math.PI * 2 / segs) * (0.55 + R() * 0.3), sa = a0 + q * Math.PI * 2 / segs, pts = [];
        for (var u = 0; u <= 8; u++) { var aa = sa + span * u / 8, jr = rr + (R() - 0.5) * lw * 0.5; pts.push([x + Math.cos(aa) * jr, y + Math.sin(aa) * jr]); }
        celStroke(ctx, pts, lw, { taper: function (p) { return Math.sin(Math.PI * p); }, rim: [3, 3] });
        if (s < steps - 2 && R() < 0.75) {
          var am = sa + span * (0.3 + R() * 0.4), tl = lw * (1.6 + R() * 1.8) * (1 - s / steps);
          var bx = x + Math.cos(am) * rr, by = y + Math.sin(am) * rr;
          celFill(ctx, bx, by, tonguePath(bx, by, am + (R() - 0.5) * 0.4, tl, lw * 0.34, (R() - 0.5) * 0.4), { rim: [3, 2], bands: [[C.deep, 1], [C.mid, 0.72], [C.core, 0.42]] });
        }
      }
      if (s < 4) for (var j = 0; j < 6; j++) { var a2 = R() * Math.PI * 2, d = rr + r * (0.2 + R() * 0.5); spark(ctx, x + Math.cos(a2) * d, y + Math.sin(a2) * d, 6 + R() * 10 * (1 - s / 4), R()); }
      return true;
    };
  }

  /** A cel lightning bolt that snaps from a to b over dur, crackles, then breaks into sparks. */
  function bolt(o) {
    var dur = o.dur || 200, amp = o.amp || 26, w = o.w || 14, hold = o.hold || 4, fade = 4, n = o.n || 16;
    return function (ctx, tq, k, R) {
      if (tq < o.t0) return false;
      var a = pt(o.a), b = pt(o.b);
      var s = (tq - o.t0) / dur, after = Math.floor((tq - o.t0 - dur) / TWOS);
      if (s > 1 && after >= hold + fade) return false;
      var front = clamp(s);
      var dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy) || 1, nx = -dy / len, ny = dx / len;
      var pts = [];
      var m = Math.max(2, Math.round(n * front));
      for (var i = 0; i <= m; i++) {
        var f = (i / m) * front, j = (i === 0 || (i === m && front >= 1)) ? 0 : (R() - 0.5) * amp * 2;
        pts.push([a[0] + dx * f + nx * j, a[1] + dy * f + ny * j]);
      }
      if (s <= 1 || after < hold) {
        var thin = s > 1 && after % 2 === 1 ? 0.55 : 1;
        celStroke(ctx, pts, w * thin, { taper: function (p) { return 0.35 + 0.65 * Math.sin(Math.PI * Math.min(1, p * 0.9 + 0.1)); }, rim: [3, 3] });
        // forks
        for (var q = 0; q < 2; q++) {
          var bi = 2 + Math.floor(R() * Math.max(1, pts.length - 3)); if (!pts[bi]) continue;
          var bp = pts[bi], dir = R() < 0.5 ? 1 : -1, fl = len * (0.06 + R() * 0.08);
          celStroke(ctx, [bp, [bp[0] + dx / len * fl * 0.6 + nx * dir * fl * 0.5, bp[1] + dy / len * fl * 0.6 + ny * dir * fl * 0.5], [bp[0] + dx / len * fl + nx * dir * fl * 0.9 + (R() - 0.5) * 8, bp[1] + dy / len * fl + ny * dir * fl * 0.9]], w * 0.45, { taper: function (p) { return 1 - p * 0.8; }, pale: false });
        }
        var hp = pts[pts.length - 1];
        spark(ctx, hp[0], hp[1], w * (s <= 1 ? 2.4 : 1.6), k * 0.5);
        return true;
      }
      var fk = after - hold;
      for (var z = 0; z < 14; z++) { var f2 = R(), sx = a[0] + dx * f2 + nx * (R() - 0.5) * amp * 2.5, sy = a[1] + dy * f2 + ny * (R() - 0.5) * amp * 2.5 - fk * 6; spark(ctx, sx, sy, (5 + R() * 8) * (1 - fk / fade), R()); }
      return true;
    };
  }

  /** A burst of flame tongues and sparks at a point (a tap). */
  function burst(o) {
    var r = o.r || 90, steps = o.steps || 8, n = o.n || 9;
    return function (ctx, tq, k, R) {
      if (tq < o.t0) return false;
      var p = pt(o.at); if (!p) return false;
      var s = Math.floor((tq - o.t0) / TWOS + 0.001);
      if (s >= steps) return false;
      var x = p[0], y = p[1], life = s / steps;
      if (s === 0) { celFill(ctx, x, y, starPath(x, y, r * 0.9, r * 0.16, 0.2), { rim: [4, 3] }); return true; }
      var R2 = rng(4242 + (o.seed || 0));
      for (var i = 0; i < n; i++) {
        var a = i / n * Math.PI * 2 + R2() * 0.5, base = r * (0.18 + 0.55 * eo(life * 1.4)), len = r * (0.55 + R2() * 0.5) * (1 - life * 0.85);
        var hw = r * 0.07 * (1 - life * 0.7);
        if (len > 2) celFill(ctx, x + Math.cos(a) * base, y + Math.sin(a) * base, tonguePath(x + Math.cos(a) * base, y + Math.sin(a) * base, a, len, hw, (R() - 0.5) * 0.3), { rim: [3, 2] });
      }
      for (var j = 0; j < 8; j++) { var a3 = R() * Math.PI * 2, d = r * (0.6 + life * 1.4) * (0.6 + R() * 0.6); spark(ctx, x + Math.cos(a3) * d, y + Math.sin(a3) * d, (7 + R() * 9) * (1 - life), R()); }
      return true;
    };
  }

  /** Spark trail following pos(ms) (a moving focus): sparks at the positions of the last few steps. */
  function trail(o) {
    var hist = o.hist || 6;
    return function (ctx, tq, k, R) {
      if (o.t0 != null && tq < o.t0) return false;
      if (o.t1 != null && tq > o.t1) return false;
      var cur = o.pos(tq); if (!cur) return false;
      var prev = o.pos(tq - TWOS * hist);
      var moving = prev && Math.hypot(cur[0] - prev[0], cur[1] - prev[1]) > 4;
      var any = false;
      if (moving) {
        var pts = []; for (var i = hist; i >= 0; i--) { var q = o.pos(tq - TWOS * i); if (q) pts.push([q[0] + (R() - 0.5) * 8, q[1] + (R() - 0.5) * 8]); }
        celStroke(ctx, pts, o.w || 5, { taper: function (p) { return p; }, pale: false, rim: [2, 2] });
        for (var j = 1; j < pts.length; j++) if (R() < 0.7) spark(ctx, pts[j][0] + (R() - 0.5) * 30, pts[j][1] + (R() - 0.5) * 30, 4 + R() * 8 * (j / pts.length), R());
        spark(ctx, cur[0], cur[1], 14 + R() * 6, k * 0.6);
        any = true;
      } else if (o.idle) {
        // crackle: two small sparks hugging the corner
        if (k % 3 !== 0) { spark(ctx, cur[0] + (R() - 0.5) * 18, cur[1] + (R() - 0.5) * 18, 4 + R() * 5, R()); any = true; }
      }
      return any;
    };
  }

  // ------------------------------------------------------------------ HUD brackets
  /** Rect of an element in frame (sec) px, crossing into an iframe if needed. */
  function rectOf(node) {
    var r = node.getBoundingClientRect();
    var win = node.ownerDocument.defaultView;
    if (win && win.frameElement) {
      var fr = win.frameElement, fb = fr.getBoundingClientRect(), k = fb.width / (fr.offsetWidth || 1);
      return { x: fb.left + r.left * k, y: fb.top + r.top * k, w: r.width * k, h: r.height * k };
    }
    return { x: r.left, y: r.top, w: r.width, h: r.height };
  }
  /**
   * hud(sec, tl, {target: el | fn(ms)->{x,y,w,h}, t0, t1, pad, label: text | [{t, text}], corner, step})
   * Corner brackets (--fx-hud) that snap on over three steps (with an overshoot) and follow the target on twos.
   */
  function hud(sec, tl, o) {
    var stepMs = o.step || TWOS, pad = o.pad == null ? 18 : o.pad, cl = o.corner || 26;
    var box = el('div', 'cfx-hud' + (o.cls ? ' ' + o.cls : ''));
    box.innerHTML = '<i class="tl"></i><i class="tr"></i><i class="bl"></i><i class="br"></i>' + (o.cross ? '<i class="cx"></i><i class="cy"></i>' : '') + '<span class="lb"></span>';
    box.style.setProperty('--cl', cl + 'px');
    sec.appendChild(box);
    var lb = box.querySelector('.lb');
    var labels = typeof o.label === 'string' ? [{ t: 0, text: o.label }] : (o.label || []);
    var lastKey = null;
    var get = typeof o.target === 'function' ? o.target : (o.target && typeof o.target.x === 'number' ? function () { return o.target; } : function () { return rectOf(o.target); });
    tl.fn(function (ms) {
      var k = stepOf(ms, stepMs), tq = k * stepMs;
      var on = tq >= o.t0 && (o.t1 == null || tq < o.t1 + 2 * stepMs);
      if (!on) { if (lastKey !== 'off') { box.style.opacity = '0'; lastKey = 'off'; } return; }
      var r = get(tq); if (!r) { box.style.opacity = '0'; lastKey = 'off'; return; }
      var s = Math.floor((tq - o.t0) / stepMs + 0.001), inflate = 0, op = 1;
      if (s === 0) { inflate = 80; op = 0.45; } else if (s === 1) { inflate = 26; op = 0.85; } else if (s === 2) { inflate = -7; }
      if (o.t1 != null && tq >= o.t1) { var e = Math.floor((tq - o.t1) / stepMs + 0.001); inflate = e === 0 ? 12 : 40; op = e === 0 ? 0.7 : 0; }
      var text = ''; for (var i = 0; i < labels.length; i++) if (tq >= labels[i].t) text = labels[i].text;
      var x = r.x - pad - inflate, y = r.y - pad - inflate, w = r.w + 2 * (pad + inflate), h = r.h + 2 * (pad + inflate);
      var key = [x, y, w, h, op, text].map(function (v) { return typeof v === 'number' ? v.toFixed(1) : v; }).join('|');
      if (key === lastKey) return;
      lastKey = key;
      box.style.left = x.toFixed(1) + 'px'; box.style.top = y.toFixed(1) + 'px'; box.style.width = w.toFixed(1) + 'px'; box.style.height = h.toFixed(1) + 'px';
      box.style.opacity = String(op);
      if (lb.textContent !== text) lb.textContent = text;
      lb.style.display = text ? '' : 'none';
    });
    return box;
  }

  // ------------------------------------------------------------------ dot grid with pulses
  /** grid(sec, tl, {origin:[x,y] | fn, pulses:[t], t0, fade, alpha, gap, speed}) behind everything in sec. */
  function grid(sec, tl, o) {
    var W = 1920, H = 1080, gap = o.gap || 36, base = o.alpha == null ? 0.075 : o.alpha, speed = o.speed || 1.5, stepMs = o.step || TWOS;
    var cv = document.createElement('canvas'); cv.width = W * DPR; cv.height = H * DPR; put(cv, 0, 0, W, H); cv.className = 'cfx-grid';
    sec.insertBefore(cv, sec.firstChild);
    var ctx = cv.getContext('2d'), last = -1;
    var cols = Math.ceil(W / gap) + 1, rows = Math.ceil(H / gap) + 1, ox = (W - (cols - 1) * gap) / 2, oy = (H - (rows - 1) * gap) / 2;
    tl.fn(function (ms) {
      var k = stepOf(ms, stepMs); if (k === last) return; last = k;
      var tq = k * stepMs;
      var fadeA = clamp((tq - (o.t0 || 0)) / (o.fade || 800));
      if (o.t1 != null) fadeA *= 1 - clamp((tq - o.t1) / 400);
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0); ctx.clearRect(0, 0, W, H);
      if (fadeA <= 0) return;
      var org = pt(o.origin) || [W / 2, H / 2];
      var live = (o.pulses || []).filter(function (t) { return tq >= t && (tq - t) * speed < 2400; });
      for (var i = 0; i < cols; i++) for (var j = 0; j < rows; j++) {
        var x = ox + i * gap, y = oy + j * gap, d = Math.hypot(x - org[0], y - org[1]);
        var a = base * fadeA * (1 - 0.55 * clamp(d / 1400)), hot = 0, rad = 1.7;
        for (var q = 0; q < live.length; q++) {
          var R = (tq - live[q]) * speed, fall = 1 - clamp(R / 2400), g = Math.exp(-Math.pow((d - R) / 46, 2)) * fall;
          a += 0.55 * g * fadeA; hot = Math.max(hot, g); rad += 1.1 * g;
        }
        if (a < 0.01) continue;
        ctx.fillStyle = hot > 0.45 ? rgba(C.mid, Math.min(1, a).toFixed(3)) : 'rgba(255,255,255,' + Math.min(1, a).toFixed(3) + ')';
        ctx.fillRect(x - rad / 2, y - rad / 2, rad, rad);
      }
    });
    return cv;
  }

  // ------------------------------------------------------------------ flash frames
  /** A flash overlay on node (or the whole frame when node is sec): white for one step, the mid band colour for one, gone. */
  function flash(parent, tl, o) {
    var f = el('div', 'cfx-flash'); parent.appendChild(f);
    if (o.radius != null) f.style.borderRadius = o.radius + 'px';
    var seq = o.seq || [['#ffffff', o.a == null ? 0.85 : o.a], [C.mid, (o.a == null ? 0.85 : o.a) * 0.35]];
    var last = null;
    tl.fn(function (ms) {
      var s = Math.floor((quant(ms, TWOS) - o.t0) / TWOS + 0.001);
      var cur = s >= 0 && s < seq.length ? seq[s] : null;
      var key = cur ? cur.join() : 'off';
      if (key === last) return; last = key;
      f.style.opacity = cur ? String(cur[1]) : '0';
      if (cur) f.style.background = cur[0];
    });
    return f;
  }

  // ------------------------------------------------------------------ the O orb, inline (same geometry as assets/o-avatar.svg)
  var orbN = 0;
  /** orb(parent, size): {wrap, back, front, ring, size}; a flame canvas goes between back and front. */
  function orb(parent, size) {
    var id = 'cfxo' + (++orbN);
    var wrap = el('div', 'cfx-orb'); wrap.style.position = 'absolute'; wrap.style.width = size + 'px'; wrap.style.height = size + 'px';
    var defs = '<defs><linearGradient id="' + id + 'b" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1a1a1d"/><stop offset=".55" stop-color="#0c0c0e"/><stop offset="1" stop-color="#050506"/></linearGradient>' +
      '<linearGradient id="' + id + 'r" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".55"/><stop offset=".5" stop-color="#fff" stop-opacity=".12"/><stop offset="1" stop-color="#fff" stop-opacity=".3"/></linearGradient>' +
      '<linearGradient id="' + id + 's" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".12"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>' +
      '<radialGradient id="' + id + 'p" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#fff" stop-opacity=".95"/><stop offset=".6" stop-color="#fff" stop-opacity=".35"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>' +
      '<filter id="' + id + 'h" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="8"/></filter>' +
      '<filter id="' + id + 'f" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="2.5"/></filter>' +
      '<clipPath id="' + id + 'c"><circle cx="128" cy="128" r="118"/></clipPath></defs>';
    var svg = function (inner) { return '<svg viewBox="0 0 256 256" width="' + size + '" height="' + size + '" xmlns="http://www.w3.org/2000/svg" style="position:absolute;left:0;top:0;overflow:visible">' + defs + inner + '</svg>'; };
    var back = el('div', ''); back.innerHTML = svg('<circle cx="128" cy="128" r="118" fill="url(#' + id + 'b)"/><ellipse cx="128" cy="52" rx="92" ry="34" fill="url(#' + id + 's)" clip-path="url(#' + id + 'c)"/><circle cx="128" cy="128" r="117.25" fill="none" stroke="url(#' + id + 'r)" stroke-width="1.5"/>');
    var front = el('div', ''); front.innerHTML = svg('<g class="ring"><circle cx="128" cy="128" r="80" fill="none" stroke="#fff" stroke-width="22" opacity=".28" filter="url(#' + id + 'h)"/><circle cx="128" cy="128" r="80" fill="none" stroke="#fff" stroke-width="10" opacity=".32" filter="url(#' + id + 'f)"/><circle cx="128" cy="128" r="80" fill="none" stroke="#fff" stroke-width="6" opacity=".96"/></g><ellipse class="spec" cx="100" cy="74" rx="18" ry="11" transform="rotate(-32 100 74)" fill="url(#' + id + 'p)"/>');
    [back, front].forEach(function (n) { n.style.position = 'absolute'; n.style.left = '0'; n.style.top = '0'; n.style.width = size + 'px'; n.style.height = size + 'px'; });
    var mid = el('div', ''); mid.style.position = 'absolute'; mid.style.left = '0'; mid.style.top = '0'; mid.style.width = size + 'px'; mid.style.height = size + 'px';
    wrap.append(back, mid, front); parent.appendChild(wrap);
    return { wrap: wrap, back: back, mid: mid, front: front, ring: front.querySelector('.ring'), spec: front.querySelector('.spec'), size: size, k: size / 256 };
  }

  window.__celfx = { C: C, TWOS: TWOS, THREES: THREES, flame: flame, ink: ink, streak: streak, impact: impact, bolt: bolt, burst: burst, trail: trail, spark: spark, hud: hud, rectOf: rectOf, grid: grid, flash: flash, orb: orb, bloom: bloom, rng: rng, quant: quant, stepOf: stepOf, eo: eo, eio: eio, clamp: clamp, put: put };
})();
