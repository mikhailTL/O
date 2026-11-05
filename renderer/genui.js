'use strict';
// O AI generative UI: validated A2UI frames -> native DOM on panel.css tokens.
// No model-produced HTML is ever rendered here; custom previews live in a sandboxed iframe.
// Plain script (CSP: script-src 'self'); exposes window.OGenUI.
(function () {
  const A2UI_VERSION = '1';
  const FRAME_TYPES = ['begin', 'update', 'data', 'end', 'sandbox'];
  const FRAME_KEYS = {
    begin: ['a2ui', 'type', 'surface', 'title'],
    update: ['a2ui', 'type', 'surface', 'components'],
    data: ['a2ui', 'type', 'surface', 'path', 'value'],
    end: ['a2ui', 'type', 'surface', 'ok'],
    sandbox: ['a2ui', 'type', 'surface', 'sandbox_id', 'html', 'css', 'js'],
  };
  const STATUSES = ['pending', 'running', 'ok', 'failed', 'skipped'];
  const TONES = ['muted', 'normal', 'error'];
  const ACTIONS = ['open-log', 'open-file', 'retry', 'approve', 'deny'];
  const TREE_NODE_KEYS = ['id', 'label', 'status', 'children'];
  const MAX_TREE_DEPTH = 12;
  // Same token rule as the engine (o_engine/genui.py _TOKEN): ids, surfaces, paths, sandbox ids, bare oui values.
  const TOKEN_RE = /^[A-Za-z0-9_.:/@-]+$/;

  // Closed component set: name -> { prop: checker }. Unknown prop -> rejected.
  const isStr = (v) => typeof v === 'string';
  const isTok = (v) => isStr(v) && TOKEN_RE.test(v);
  const isIdList = (v) => Array.isArray(v) && v.every(isTok);
  const isUnit = (v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1;
  const isObj = (v) => !!v && typeof v === 'object' && !Array.isArray(v);
  const oneOf = (list) => (v) => list.includes(v);
  function isTreeNodes(nodes, depth) {
    if (!Array.isArray(nodes)) return false;
    if ((depth || 0) > MAX_TREE_DEPTH) return false;
    return nodes.every((n) => isObj(n) && Object.keys(n).every((k) => TREE_NODE_KEYS.includes(k))
      && isTok(n.id) && isStr(n.label)
      && (n.status === undefined || STATUSES.includes(n.status))
      && (n.children === undefined || isTreeNodes(n.children, (depth || 0) + 1)));
  }
  // Defaults mirror the engine's pydantic models: a key at its default is omitted in oui and filled on decode.
  const COMPONENTS = {
    Column: { children: { check: isIdList, def: () => [] } },
    Row: { children: { check: isIdList, def: () => [] } },
    Card: { title: { check: isStr, def: () => '' }, children: { check: isIdList, def: () => [] } },
    Text: { text: { check: isStr, required: true }, tone: { check: oneOf(TONES), def: () => 'normal' } },
    Step: { label: { check: isStr, required: true }, status: { check: oneOf(STATUSES), def: () => 'pending' }, detail: { check: isStr, def: () => '' }, progress: { check: (v) => v === null || isUnit(v) } }, // optional; the engine omits it when unset (null tolerated)
    Tree: { nodes: { check: (v) => isTreeNodes(v, 0), def: () => [] } },
    Progress: { value: { check: isUnit, required: true }, label: { check: isStr, def: () => '' } },
    KeyValue: { items: { check: (v) => Array.isArray(v) && v.every((it) => Array.isArray(it) && it.length === 2 && isStr(it[0]) && isStr(it[1])), def: () => [] } },
    Button: { label: { check: isStr, required: true }, action: { check: oneOf(ACTIONS), required: true }, payload: { check: isObj, def: () => ({}) } },
    Preview: { sandbox_id: { check: isTok, required: true }, height: { check: (v) => Number.isInteger(v) && v >= 16 && v <= 4000, def: () => 240 } },
  };
  const STRING_PROPS = ['title', 'text', 'tone', 'label', 'status', 'detail', 'action', 'sandbox_id'];
  const FRAME_DEFAULTS = { begin: { title: '' }, sandbox: { css: '', js: '' } };

  function normalizeNodes(nodes) {
    return (nodes || []).map((n) => ({ id: n.id, label: n.label, status: n.status === undefined ? 'pending' : n.status, children: normalizeNodes(n.children) }));
  }
  /** A validated frame with every defaulted key present, as the engine's wire form has it. */
  function normalizeFrame(frame) {
    const out = Object.assign({}, frame);
    const defs = FRAME_DEFAULTS[frame.type] || {};
    for (const k of Object.keys(defs)) if (out[k] === undefined) out[k] = defs[k];
    if (frame.type === 'update') {
      out.components = frame.components.map((c) => {
        const type = Object.keys(c.component)[0];
        const props = Object.assign({}, c.component[type]);
        const spec = COMPONENTS[type];
        for (const k of Object.keys(spec)) if (props[k] === undefined && spec[k].def) props[k] = spec[k].def();
        if (type === 'Step' && props.progress === null) delete props.progress;
        if (type === 'Tree') props.nodes = normalizeNodes(props.nodes);
        return { id: c.id, component: { [type]: props } };
      });
    }
    return out;
  }

  // ------------------------------------------------------------ validator

  function fail(error) { return { ok: false, error }; }

  function validateComponent(entry, i) {
    if (!isObj(entry)) return `component ${i}: not an object`;
    const keys = Object.keys(entry);
    if (!keys.every((k) => k === 'id' || k === 'component')) return `component ${i}: unknown key "${keys.find((k) => k !== 'id' && k !== 'component')}"`;
    if (!isTok(entry.id)) return `component ${i}: bad id`;
    if (!isObj(entry.component)) return `component "${entry.id}": missing component`;
    const types = Object.keys(entry.component);
    if (types.length !== 1) return `component "${entry.id}": expected exactly one component type`;
    const type = types[0];
    const spec = COMPONENTS[type];
    if (!spec) return `component "${entry.id}": unknown component "${type}"`;
    const props = entry.component[type];
    if (!isObj(props)) return `component "${entry.id}": ${type} props must be an object`;
    for (const k of Object.keys(props)) {
      if (!spec[k]) return `component "${entry.id}": unknown ${type} property "${k}"`;
      if (!spec[k].check(props[k])) return `component "${entry.id}": invalid ${type}.${k}`;
    }
    for (const k of Object.keys(spec)) {
      if (spec[k].required && props[k] === undefined) return `component "${entry.id}": ${type} needs "${k}"`;
    }
    return '';
  }

  function validateFrame(frame) {
    if (!isObj(frame)) return fail('frame is not an object');
    if (frame.a2ui !== A2UI_VERSION) return fail('unsupported a2ui version');
    if (!FRAME_TYPES.includes(frame.type)) return fail(`unknown frame type "${String(frame.type)}"`);
    if (!isTok(frame.surface) || !/^(job|chat):/.test(frame.surface)) return fail('surface must look like job:<id> or chat:<id>');
    const allowed = FRAME_KEYS[frame.type];
    const extra = Object.keys(frame).find((k) => !allowed.includes(k));
    if (extra) return fail(`unknown ${frame.type} frame key "${extra}"`);
    switch (frame.type) {
      case 'begin':
        if (frame.title !== undefined && !isStr(frame.title)) return fail('title must be a string');
        return { ok: true };
      case 'update': {
        if (!Array.isArray(frame.components) || !frame.components.length) return fail('update needs components');
        const seen = new Set();
        for (let i = 0; i < frame.components.length; i++) {
          const err = validateComponent(frame.components[i], i);
          if (err) return fail(err);
          if (seen.has(frame.components[i].id)) return fail(`duplicate component id "${frame.components[i].id}"`);
          seen.add(frame.components[i].id);
        }
        return { ok: true };
      }
      case 'data':
        if (!isTok(frame.path) || !frame.path.includes('.')) return fail('data path must be dotted, e.g. steps.0.status');
        if (frame.value === undefined) return fail('data needs a value');
        return { ok: true };
      case 'end':
        if (typeof frame.ok !== 'boolean') return fail('end needs ok: true|false');
        return { ok: true };
      case 'sandbox':
        if (!isTok(frame.sandbox_id)) return fail('sandbox needs a token sandbox_id');
        if (!isStr(frame.html)) return fail('sandbox needs html');
        if (frame.css !== undefined && !isStr(frame.css)) return fail('sandbox css must be a string');
        if (frame.js !== undefined && !isStr(frame.js)) return fail('sandbox js must be a string');
        return { ok: true };
      default:
        return fail('unknown frame type');
    }
  }

  // ------------------------------------------------------------ oui (compact grammar)
  // Mirrors o_engine/genui.py `oui` exactly. One line per component: <Type>#<id>[<k>=<v>;…]{<child ids>}
  // Frame lines: @begin <surface> <title> | @update <surface> (component lines follow)
  //              @data <surface> <path> <json> | @end <surface> true|false | @sandbox <surface> <id> <json>
  // Values are bare when they match TOKEN_RE and JSON otherwise; keys at their default are omitted.

  const TYPE_SHORT = { Column: 'Col', Row: 'Row', Card: 'Card', Text: 'Txt', Step: 'Step', Tree: 'Tree', Progress: 'Prog', KeyValue: 'KV', Button: 'Btn', Preview: 'Prev' };
  const TYPE_LONG = Object.fromEntries(Object.entries(TYPE_SHORT).map(([k, v]) => [v, k]));
  const KEYS = {
    Column: {}, Row: {},
    Card: { title: 't' },
    Text: { text: 't', tone: 'tn' },
    Step: { label: 'l', status: 'st', detail: 'd', progress: 'p' },
    Tree: { nodes: 'n' },
    Progress: { value: 'v', label: 'l' },
    KeyValue: { items: 'i' },
    Button: { label: 'l', action: 'a', payload: 'pl' },
    Preview: { sandbox_id: 's', height: 'h' },
  };
  const KEYS_BACK = Object.fromEntries(Object.keys(KEYS).map((t) => [t, Object.fromEntries(Object.entries(KEYS[t]).map(([k, v]) => [v, k]))]));
  const STATUS_SHORT = { pending: 'pend', running: 'run', ok: 'ok', failed: 'fail', skipped: 'skip' };
  const STATUS_LONG = Object.fromEntries(Object.entries(STATUS_SHORT).map(([k, v]) => [v, k]));

  const sameJson = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  function enc(v) { return isTok(v) ? v : JSON.stringify(v); }
  function dec(token, asStr) {
    if (/^["\[{]/.test(token) || !asStr) {
      try { return JSON.parse(token); } catch (_) { throw new Error(`oui: bad value ${token.slice(0, 40)}`); }
    }
    return token;
  }

  function encodeComponent(entry) {
    const type = Object.keys(entry.component)[0];
    const props = entry.component[type];
    const spec = COMPONENTS[type];
    const pairs = [];
    for (const [field, short] of Object.entries(KEYS[type])) {
      let v = props[field];
      if (v === undefined || v === null) continue;
      if (spec[field].def && sameJson(v, spec[field].def())) continue;
      if (type === 'Tree') v = normalizeNodes(v);
      if (type === 'Step' && field === 'status') v = STATUS_SHORT[v];
      pairs.push(`${short}=${enc(v)}`);
    }
    let line = `${TYPE_SHORT[type]}#${entry.id}`;
    if (pairs.length) line += `[${pairs.join(';')}]`;
    if (props.children && props.children.length) line += `{${props.children.join(' ')}}`;
    return line;
  }

  function encodeOui(frames) {
    const out = [];
    for (const f of frames || []) {
      const v = validateFrame(f);
      if (!v.ok) throw new Error(`encodeOui: ${v.error}`);
      switch (f.type) {
        case 'begin': out.push(`@begin ${f.surface} ${enc(f.title === undefined ? '' : f.title)}`); break;
        case 'update': out.push(`@update ${f.surface}`); for (const c of f.components) out.push(encodeComponent(c)); break;
        case 'data': out.push(`@data ${f.surface} ${f.path} ${JSON.stringify(f.value)}`); break;
        case 'end': out.push(`@end ${f.surface} ${f.ok ? 'true' : 'false'}`); break;
        case 'sandbox': out.push(`@sandbox ${f.surface} ${f.sandbox_id} ${JSON.stringify({ html: f.html, css: f.css || '', js: f.js || '' })}`); break;
        default: break;
      }
    }
    return out.length ? out.join('\n') + '\n' : '';
  }

  // Value scanner inside [...]: a JSON string, a JSON array/object, or a bare run up to ; or ].
  function scanValue(text, pos) {
    if (pos >= text.length) throw new Error('oui: missing value');
    const ch = text[pos];
    if (ch === '"') {
      for (let i = pos + 1; i < text.length; i++) {
        if (text[i] === '\\') { i++; continue; }
        if (text[i] === '"') return [text.slice(pos, i + 1), i + 1];
      }
      throw new Error('oui: unterminated string');
    }
    if (ch === '[' || ch === '{') {
      let depth = 0, inStr = false;
      for (let i = pos; i < text.length; i++) {
        const c = text[i];
        if (inStr) { if (c === '\\') i++; else if (c === '"') inStr = false; continue; }
        if (c === '"') inStr = true;
        else if (c === '[' || c === '{') depth++;
        else if (c === ']' || c === '}') { depth--; if (depth === 0) return [text.slice(pos, i + 1), i + 1]; }
      }
      throw new Error('oui: bad JSON value');
    }
    let i = pos;
    while (i < text.length && text[i] !== ';' && text[i] !== ']') i++;
    return [text.slice(pos, i), i];
  }

  function decodeComponentLine(line) {
    const m = /^([A-Za-z]+)#([A-Za-z0-9_.:/@-]+)/.exec(line);
    if (!m) throw new Error(`oui: bad component line "${line.slice(0, 40)}"`);
    const type = TYPE_LONG[m[1]];
    if (!type) throw new Error(`oui: unknown component "${m[1]}"`);
    const id = m[2];
    const props = {};
    let pos = m[0].length;
    if (line[pos] === '[') {
      pos++;
      for (;;) {
        const eq = line.indexOf('=', pos);
        if (eq < 0) throw new Error(`oui: bad property in "${line.slice(0, 40)}"`);
        const key = line.slice(pos, eq).trim();
        const field = KEYS_BACK[type][key];
        if (!field) throw new Error(`oui: unknown property "${key}" for ${type}`);
        const [raw, next] = scanValue(line, eq + 1);
        pos = next;
        let v = dec(raw, STRING_PROPS.includes(field));
        if (type === 'Step' && field === 'status' && isStr(v)) v = STATUS_LONG[v] || v;
        props[field] = v;
        if (pos >= line.length) throw new Error('oui: unterminated [');
        if (line[pos] === ';') { pos++; continue; }
        if (line[pos] === ']') { pos++; break; }
        throw new Error(`oui: unexpected "${line[pos]}"`);
      }
    }
    if (line[pos] === '{') {
      const end = line.indexOf('}', pos);
      if (end < 0) throw new Error('oui: unterminated {');
      props.children = line.slice(pos + 1, end).trim().split(/\s+/).filter(Boolean);
      pos = end + 1;
    }
    if (line.slice(pos).trim()) throw new Error(`oui: trailing text "${line.slice(pos).trim().slice(0, 20)}"`);
    return { id, component: { [type]: props } };
  }

  function decodeOui(text) {
    const frames = [];
    let update = null;
    const push = (f) => {
      const v = validateFrame(f);
      if (!v.ok) throw new Error(`oui: ${v.error}`);
      frames.push(normalizeFrame(f));
    };
    const flush = () => { if (update) { if (!update.components.length) throw new Error('oui: @update with no components'); push(update); update = null; } };
    for (const rawLine of String(text || '').split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line) continue;
      if (line[0] === '@') {
        flush();
        const sp = line.indexOf(' ');
        const head = sp < 0 ? line : line.slice(0, sp);
        let rest = sp < 0 ? '' : line.slice(sp + 1).trim();
        if (head === '@update') { update = { a2ui: A2UI_VERSION, type: 'update', surface: rest, components: [] }; continue; }
        const sp2 = rest.indexOf(' ');
        const surface = sp2 < 0 ? rest : rest.slice(0, sp2);
        rest = sp2 < 0 ? '' : rest.slice(sp2 + 1).trim();
        if (head === '@begin') {
          push({ a2ui: A2UI_VERSION, type: 'begin', surface, title: rest ? String(dec(rest, true)) : '' });
        } else if (head === '@data') {
          const sp3 = rest.indexOf(' ');
          if (sp3 < 0) throw new Error('oui: @data needs path and value');
          let value;
          try { value = JSON.parse(rest.slice(sp3 + 1)); } catch (_) { throw new Error('oui: bad @data value'); }
          push({ a2ui: A2UI_VERSION, type: 'data', surface, path: rest.slice(0, sp3), value });
        } else if (head === '@end') {
          if (rest !== 'true' && rest !== 'false') throw new Error('oui: @end needs true|false');
          push({ a2ui: A2UI_VERSION, type: 'end', surface, ok: rest === 'true' });
        } else if (head === '@sandbox') {
          const sp3 = rest.indexOf(' ');
          if (sp3 < 0) throw new Error('oui: @sandbox needs id and body');
          let obj;
          try { obj = JSON.parse(rest.slice(sp3 + 1)); } catch (_) { throw new Error('oui: bad @sandbox body'); }
          if (!isObj(obj)) throw new Error('oui: @sandbox body must be an object');
          push({ a2ui: A2UI_VERSION, type: 'sandbox', surface, sandbox_id: rest.slice(0, sp3), html: obj.html === undefined ? '' : obj.html, css: obj.css === undefined ? '' : obj.css, js: obj.js === undefined ? '' : obj.js });
        } else {
          throw new Error(`oui: unknown directive ${head}`);
        }
        continue;
      }
      if (!update) throw new Error('oui: component line outside @update');
      update.components.push(decodeComponentLine(line));
    }
    flush();
    return frames;
  }

  // ------------------------------------------------------------ rendering

  // The policy preview.html enforces on itself (its <meta http-equiv="Content-Security-Policy">). Kept here verbatim
  // for the action-log record only: the frame's CSP comes from that file, never from a message, so this constant is
  // never posted into the sandbox. Change both together.
  const SANDBOX_CSP = "default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-dl+R7e0xY5dP6T2NwG+yoGvXYsQXQMgODso12iw1mdc=' 'strict-dynamic'; img-src data:";
  const STATUS_LABEL = { pending: 'pending', running: 'running', ok: 'ok', failed: 'failed', skipped: 'skipped' };

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function pill(status) {
    const p = el('span', 'gu-pill', STATUS_LABEL[status] || status);
    p.dataset.status = status;
    return p;
  }

  function logNote(summary, detail) {
    const o = window.o;
    if (o && typeof o.logNote === 'function') { try { o.logNote(summary, detail); } catch (_) {} }
  }

  class Surface {
    constructor(id) {
      this.id = id;
      this.title = '';
      this.status = 'running';   // running | ok | failed
      this.order = [];           // component ids in arrival order
      this.components = new Map(); // id -> { type, props }
      this.sandboxes = new Map();  // sandbox_id -> { html, css, js }
      this.iframes = new Map();    // sandbox_id -> iframe (kept across re-renders)
      this.root = null;
      this.body = null;
      this.head = null;
      this.frames = [];
      this._onMessage = null;
    }

    mount(container) {
      this.root = el('section', 'gu-surface');
      this.root.dataset.surface = this.id;
      this.root.dataset.status = this.status;
      this.head = el('header', 'gu-head');
      this.head.appendChild(el('span', 'gu-title', this.title || this.id));
      this.head.appendChild(pill(this.status));
      this.body = el('div', 'gu-body');
      this.root.appendChild(this.head);
      this.root.appendChild(this.body);
      container.appendChild(this.root);
      this._onMessage = (e) => this._message(e);
      window.addEventListener('message', this._onMessage);
      this.render();
      return this.root;
    }

    unmount() {
      if (this._onMessage) window.removeEventListener('message', this._onMessage);
      this._onMessage = null;
      if (this.root) this.root.remove();
      this.root = this.body = this.head = null;
    }

    /** Apply a frame. Returns { ok, error }; invalid frames are logged and ignored. */
    apply(frame) {
      const v = validateFrame(frame);
      if (!v.ok) { logNote(`GenUI frame rejected: ${v.error}`, { surface: this.id, type: frame && frame.type }); return v; }
      if (frame.surface !== this.id) return fail('frame is for another surface');
      frame = normalizeFrame(frame);
      this.frames.push(frame);
      switch (frame.type) {
        case 'begin':
          this.title = frame.title || '';
          this.status = 'running';
          this._renderHead();
          break;
        case 'update':
          for (const c of frame.components) {
            const type = Object.keys(c.component)[0];
            if (!this.components.has(c.id)) this.order.push(c.id);
            this.components.set(c.id, { type, props: Object.assign({}, c.component[type]) });
          }
          this.render();
          break;
        case 'data': {
          const r = this._patch(frame.path, frame.value);
          if (r.error) { logNote(`GenUI data patch ignored: ${r.error}`, { surface: this.id, path: frame.path }); return fail(r.error); }
          this._renderOne(r.id);
          break;
        }
        case 'end':
          this.status = frame.ok ? 'ok' : 'failed';
          this._renderHead();
          if (!this.components.size) this.render();
          break;
        case 'sandbox':
          this.sandboxes.set(frame.sandbox_id, { html: frame.html, css: frame.css || '', js: frame.js || '' });
          this.iframes.delete(frame.sandbox_id); // content changed: rebuild that preview only
          for (const [id, c] of this.components) if (c.type === 'Preview' && c.props.sandbox_id === frame.sandbox_id) this._renderOne(id);
          break;
        default: break;
      }
      return { ok: true };
    }

    /** Resolve "<id>.<prop>", "steps.<n>.<prop>" and "<treeId>.nodes.<nodeId>.<prop>". */
    _patch(path, value) {
      const parts = path.split('.');
      let target = null;
      let rest;
      let id;
      const stepsCol = this.components.get('steps');
      if (parts[0] === 'steps' && /^\d+$/.test(parts[1]) && !(stepsCol && stepsCol.type !== 'Step' && parts.length === 2)) {
        // Nth child of the component with id "steps" (the engine's layout); else the Nth Step in document order.
        const n = Number(parts[1]);
        if (stepsCol && Array.isArray(stepsCol.props.children)) {
          id = stepsCol.props.children[n];
          target = id ? this.components.get(id) : null;
        } else {
          const steps = this._stepsInOrder();
          target = steps[n];
          id = target ? target.id : undefined;
        }
        rest = parts.slice(2);
        if (!target) return { error: `no step #${parts[1]}` };
      } else {
        id = parts[0];
        target = this.components.get(id);
        rest = parts.slice(1);
        if (!target) return { error: `no component "${id}"` };
      }
      const bad = (error) => ({ id, error });
      if (target.type === 'Tree' && rest[0] === 'nodes' && rest.length === 3) {
        const node = findNode(target.props.nodes, rest[1]);
        if (!node) return bad(`no tree node "${rest[1]}"`);
        const prop = rest[2];
        if (!TREE_NODE_KEYS.includes(prop) || prop === 'id' || prop === 'children') return bad(`tree node property "${prop}" is not patchable`);
        if (prop === 'status' && !STATUSES.includes(value)) return bad('invalid status');
        if (prop === 'label' && !isStr(value)) return bad('label must be a string');
        node[prop] = value;
        return { id };
      }
      if (rest.length !== 1) return bad(`bad path "${path}"`);
      const prop = rest[0];
      const spec = COMPONENTS[target.type][prop];
      if (!spec) return bad(`unknown ${target.type} property "${prop}"`);
      if (prop === 'children') return bad('children are not patchable');
      if (!spec.check(value)) return bad(`invalid ${target.type}.${prop}`);
      target.props[prop] = value;
      return { id };
    }

    /** Steps in document order: walk from the roots; unreachable steps follow in arrival order. */
    _stepsInOrder() {
      const out = [];
      const seen = new Set();
      const visit = (id) => {
        if (seen.has(id)) return;
        seen.add(id);
        const c = this.components.get(id);
        if (!c) return;
        if (c.type === 'Step') out.push({ id, type: c.type, props: c.props });
        for (const ch of c.props.children || []) visit(ch);
      };
      for (const id of this._roots()) visit(id);
      for (const id of this.order) visit(id);
      return out;
    }

    _roots() {
      const referenced = new Set();
      for (const c of this.components.values()) for (const ch of c.props.children || []) referenced.add(ch);
      return this.order.filter((id) => !referenced.has(id));
    }

    _renderHead() {
      if (!this.root) return;
      this.root.dataset.status = this.status;
      this.head.querySelector('.gu-title').textContent = this.title || this.id;
      this.head.querySelector('.gu-pill').replaceWith(pill(this.status));
    }

    _collapsed() {
      const closed = new Set();
      if (this.body) this.body.querySelectorAll('.gu-tree button[aria-expanded="false"]').forEach((b) => closed.add(b.dataset.node));
      return closed;
    }

    /** Full rebuild (structure changed). Kept iframes move with their component; a moved iframe reloads. */
    render() {
      if (!this.root) return;
      this._renderHead();
      const closed = this._collapsed();
      const frag = document.createDocumentFragment();
      const seen = new Set();
      for (const id of this._roots()) frag.appendChild(this._render(id, seen, 0, closed));
      this.body.replaceChildren(frag);
      if (!this.components.size) this.body.appendChild(el('div', 'gu-empty', this.status === 'running' ? 'Working…' : 'Nothing to show'));
    }

    /** Re-render one component in place (data patch / sandbox arrival); falls back to a full render. */
    _renderOne(id) {
      if (!this.root) return;
      const cur = this.body.querySelector(`[data-id="${CSS.escape(id)}"]`);
      if (!cur) { this.render(); return; }
      cur.replaceWith(this._render(id, new Set(), 0, this._collapsed()));
    }

    _render(id, seen, depth, closed) {
      if (seen.has(id) || depth > 24) return el('span', 'gu-missing', '');
      seen.add(id);
      const c = this.components.get(id);
      if (!c) return el('span', 'gu-missing', `(${id})`);
      const p = c.props;
      let node;
      switch (c.type) {
        case 'Column':
        case 'Row':
          node = el('div', c.type === 'Row' ? 'gu-row' : 'gu-col');
          for (const ch of p.children) node.appendChild(this._render(ch, seen, depth + 1, closed));
          break;
        case 'Card':
          node = el('div', 'gu-card');
          if (p.title) node.appendChild(el('div', 'gu-card-title', p.title));
          for (const ch of p.children) node.appendChild(this._render(ch, seen, depth + 1, closed));
          break;
        case 'Text':
          node = el('p', 'gu-text', p.text);
          node.dataset.tone = p.tone || 'normal';
          break;
        case 'Step': {
          node = el('div', 'gu-step');
          node.dataset.status = p.status;
          const line = el('div', 'gu-step-line');
          line.appendChild(pill(p.status));
          line.appendChild(el('span', 'gu-step-label', p.label));
          node.appendChild(line);
          if (p.detail) node.appendChild(el('div', 'gu-step-detail', p.detail));
          if (typeof p.progress === 'number') node.appendChild(progressBar(p.progress));
          if (p.status === 'running' && typeof p.progress !== 'number') node.classList.add('gu-step-busy');
          break;
        }
        case 'Tree':
          node = el('div', 'gu-tree');
          node.appendChild(treeList(p.nodes, closed, 0));
          break;
        case 'Progress':
          node = el('div', 'gu-progress');
          if (p.label) node.appendChild(el('span', 'gu-progress-label', p.label));
          node.appendChild(progressBar(p.value));
          break;
        case 'KeyValue': {
          node = el('dl', 'gu-kv');
          for (const [k, v] of p.items) { node.appendChild(el('dt', '', k)); node.appendChild(el('dd', '', v)); }
          break;
        }
        case 'Button': {
          node = el('button', 'gu-btn', p.label);
          node.type = 'button';
          node.dataset.action = p.action;
          const payload = p.payload || {};
          const surface = this.id;
          node.addEventListener('click', () => {
            node.dispatchEvent(new CustomEvent('o-genui-action', { bubbles: true, detail: { action: p.action, payload, surface, componentId: id } }));
          });
          break;
        }
        case 'Preview':
          node = this._preview(p);
          break;
        default:
          node = el('span', 'gu-missing', '');
      }
      node.dataset.id = id;
      return node;
    }

    _preview(p) {
      const wrap = el('div', 'gu-preview');
      const sb = this.sandboxes.get(p.sandbox_id);
      const height = typeof p.height === 'number' ? p.height : 240;
      if (window.OGenUI && window.OGenUI.sandboxEnabled === false) { wrap.classList.add('gu-preview-off'); wrap.appendChild(el('div', 'gu-empty', 'Preview hidden (sandboxed previews are off in settings)')); return wrap; }
      wrap.style.height = height + 'px';
      if (!sb) { wrap.appendChild(el('div', 'gu-empty', 'Preview not ready')); return wrap; }
      let iframe = this.iframes.get(p.sandbox_id);
      if (!iframe) {
        // The frame loads preview.html (its own CSP, no inheritance from the panel) with an opaque origin;
        // the document arrives by postMessage and the frame can only answer with {type:'o-ui-event'}.
        iframe = document.createElement('iframe');
        iframe.className = 'gu-frame';
        iframe.setAttribute('sandbox', 'allow-scripts');
        iframe.setAttribute('referrerpolicy', 'no-referrer');
        iframe.setAttribute('title', 'Preview ' + p.sandbox_id);
        iframe.dataset.sandboxId = p.sandbox_id;
        const doc = { type: 'o-ui-load', html: sb.html, css: sb.css, js: sb.js };
        iframe.addEventListener('load', () => { try { iframe.contentWindow.postMessage(doc, '*'); } catch (_) {} });
        iframe.src = 'preview.html';
        this.iframes.set(p.sandbox_id, iframe);
        logNote(`Preview sandbox built: ${p.sandbox_id}`, { surface: this.id, sandbox_id: p.sandbox_id, sandbox: 'allow-scripts', csp: SANDBOX_CSP, html_bytes: sb.html.length, css_bytes: sb.css.length, js_bytes: sb.js.length });
      }
      iframe.style.height = height + 'px';
      wrap.appendChild(iframe);
      return wrap;
    }

    _message(e) {
      if (!this.root || !e || !e.data || typeof e.data !== 'object' || e.data.type !== 'o-ui-event') return;
      let sandboxId = null;
      for (const [sid, f] of this.iframes) if (f.contentWindow && e.source === f.contentWindow) { sandboxId = sid; break; }
      if (!sandboxId) return;
      const name = isStr(e.data.name) ? e.data.name.slice(0, 80) : '';
      const payload = isObj(e.data.payload) ? e.data.payload : {};
      logNote(`Preview event: ${name || '(unnamed)'}`, { surface: this.id, sandbox_id: sandboxId, name, payload });
      this.root.dispatchEvent(new CustomEvent('o-genui-event', { bubbles: true, detail: { surface: this.id, sandboxId, name, payload } }));
    }
  }

  function findNode(nodes, id) {
    for (const n of nodes || []) {
      if (n.id === id) return n;
      const hit = findNode(n.children, id);
      if (hit) return hit;
    }
    return null;
  }

  function progressBar(value) {
    const bar = el('div', 'gu-bar');
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', '100');
    bar.setAttribute('aria-valuenow', String(Math.round(value * 100)));
    const fill = el('div', 'gu-bar-fill');
    fill.style.transform = `scaleX(${Math.max(0, Math.min(1, value))})`;
    bar.appendChild(fill);
    return bar;
  }

  function treeList(nodes, closed, depth) {
    const ul = el('ul', 'gu-tree-list');
    for (const n of nodes) {
      const li = el('li', 'gu-node');
      li.dataset.status = n.status || '';
      const line = el('div', 'gu-node-line');
      const kids = n.children && n.children.length && depth < MAX_TREE_DEPTH;
      if (kids) {
        const btn = el('button', 'gu-twisty');
        btn.type = 'button';
        btn.dataset.node = n.id;
        const expanded = !closed.has(n.id);
        btn.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        btn.setAttribute('aria-label', (expanded ? 'Collapse ' : 'Expand ') + n.label);
        btn.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M7 5l6 5-6 5"/></svg>';
        line.appendChild(btn);
        const sub = treeList(n.children, closed, depth + 1);
        sub.hidden = !expanded;
        btn.addEventListener('click', () => {
          const o = btn.getAttribute('aria-expanded') === 'true';
          btn.setAttribute('aria-expanded', o ? 'false' : 'true');
          btn.setAttribute('aria-label', (o ? 'Expand ' : 'Collapse ') + n.label);
          sub.hidden = o;
        });
        li.appendChild(line);
        li.appendChild(sub);
      } else {
        line.appendChild(el('span', 'gu-twisty gu-twisty-leaf'));
        li.appendChild(line);
      }
      line.appendChild(pill(n.status || 'pending'));
      line.appendChild(el('span', 'gu-node-label', n.label));
      ul.appendChild(li);
    }
    return ul;
  }

  window.OGenUI = { validateFrame, normalizeFrame, decodeOui, encodeOui, Surface, sandboxEnabled: true, COMPONENTS: Object.keys(COMPONENTS), ACTIONS: ACTIONS.slice(), STATUSES: STATUSES.slice(), SANDBOX_CSP };
})();
