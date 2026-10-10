/* Headless smoke test for assets/pi-lamp.js (fake DOM, no jsdom needed).
   Verifies: stale-snapshot handling, immediate acknowledge, listener balance,
   skew tolerance, recovery, non-JSON handling. */
const fs = require('fs');
const vm = require('vm');

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = []; this.dataset = {}; this.style = {}; this._attrs = {};
    this._listeners = {}; this.className = ''; this._text = ''; this.parentNode = null;
    this.title = ''; this.type = ''; this.href = '';
  }
  set textContent(v) { this._detach(); this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  _detach() { for (const c of this.children) c.parentNode = null; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  remove() {
    if (this.parentNode) {
      const i = this.parentNode.children.indexOf(this);
      if (i >= 0) this.parentNode.children.splice(i, 1);
      this.parentNode = null;
    }
  }
  setAttribute(k, v) { this._attrs[k] = String(v); }
  getAttribute(k) { return (k in this._attrs) ? this._attrs[k] : null; }
  contains(n) { while (n) { if (n === this) return true; n = n.parentNode; } return false; }
  get offsetHeight() { return 200; }
  getBoundingClientRect() { return { top: 10, left: 10, bottom: 20, right: 20 }; }
  addEventListener(t, f) { (this._listeners[t] = this._listeners[t] || []).push(f); }
  removeEventListener(t, f) {
    const a = this._listeners[t] || []; const i = a.indexOf(f);
    if (i >= 0) a.splice(i, 1);
  }
  fire(t, ev) {
    const e = Object.assign({ preventDefault() {}, stopPropagation() {}, target: this }, ev);
    if (t === 'click' && typeof this.onclick === 'function') this.onclick(e);
    for (const f of (this._listeners[t] || []).slice()) f(e);
  }
  all() { const out = []; const walk = n => { for (const c of n.children) { out.push(c); walk(c); } }; walk(this); return out; }
  matchesSimple(tok) {
    // tok like '#id', '.cls', 'tag', or base + '[attr="val"]' / '[attr]'
    let m = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(tok);
    if (m) { const k = m[1], v = m[2]; const attr = k === 'id' ? this.id : this.getAttribute(k); return v === undefined ? attr != null : attr === v; }
    let base = tok, attrs = [];
    const re = /\[([\w-]+)(?:="([^"]*)")?\]/g; let mm;
    while ((mm = re.exec(tok))) attrs.push([mm[1], mm[2]]);
    base = tok.replace(re, '');
    if (base.startsWith('#')) return this.id === base.slice(1);
    if (base.startsWith('.')) return this.className.split(/\s+/).includes(base.slice(1));
    if (base) { if (this.tagName !== base.toUpperCase()) return false; }
    for (const [k, v] of attrs) {
      const attr = k === 'id' ? this.id : (k === 'class' ? this.className : this.getAttribute(k));
      if (v === undefined) { if (attr == null) return false; }
      else if (attr !== v) return false;
    }
    return true;
  }
  matches(sel) { return sel.split(',').map(s => s.trim()).some(s => this.matchesSimple(s)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    for (const part of sel.split(',').map(s => s.trim())) {
      if (part.startsWith(':scope >')) {
        const sub = part.slice(':scope >'.length).trim();
        for (const c of this.children) if (c.matchesSimple(sub)) out.push(c);
        continue;
      }
      const toks = part.split(/\s+/);
      for (const n of this.all()) {
        if (!n.matchesSimple(toks[toks.length - 1])) continue;
        let cur = n.parentNode, ok = true;
        for (let i = toks.length - 2; i >= 0; i--) {
          let found = false;
          while (cur) { if (cur.matchesSimple(toks[i])) { found = true; cur = cur.parentNode; break; } cur = cur.parentNode; }
          if (!found) { ok = false; break; }
        }
        if (ok && !out.includes(n)) out.push(n);
      }
    }
    return out;
  }
}

function makeDoc() {
  const doc = new El('#document');
  doc.readyState = 'complete';
  doc.createElement = (t) => new El(t);
  doc.getElementById = (id) => doc.all().find(e => e.id === id) || null;
  const panel = new El('div'); panel.id = 'panelChat'; doc.appendChild(panel);
  const bar = new El('div'); bar.className = 'app-titlebar-inner';
  const barIn = new El('div'); barIn.className = 'app-titlebar-inner'; doc.appendChild(barIn);
  const body = new El('body'); doc.body = body; doc.appendChild(body);
  function row(cls, sid) {
    const r = new El('div'); r.className = cls; r.dataset.sid = sid;
    const t = new El('div'); t.className = 'session-title-row'; r.appendChild(t);
    panel.appendChild(r); return r;
  }
  return { doc, body, chipHost: barIn, row: row, rowA: row('session-item', 'A'), rowB: row('session-item', 'B') };
}

/* ── payload builders (writer 1.1 shape unless stated otherwise) ───────── */
const isoAgo = (s) => new Date(Date.now() - s * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');

function run(id, over) {                       // active job, group 'active'
  return Object.assign({
    job_id: id, view: 'running', status: 'running', sessions: ['A'], group: 'active',
    finished_at: null, finished_age_s: null, duration_s: 30, activity_age_s: 4,
    turn_n: 1, turn_kind: 'task', turns_total: 1, exit_code: null,
    created_at: isoAgo(60), updated_at: isoAgo(5),
  }, over);
}
function term(id, view, group, ageS, over) {   // terminal job of a given group
  return Object.assign({
    job_id: id, view, status: view, sessions: ['A'], group,
    finished_at: isoAgo(ageS), finished_age_s: ageS, duration_s: 60,
    created_at: isoAgo(ageS + 60), updated_at: isoAgo(ageS),
    exit_code: view === 'done' ? 0 : 1, error: view === 'failed' ? 'pi exited 1' : null,
    delivery: view === 'done' ? { attempted: true, ok: true, channel: 'webui:A', reason: 'ok' } : null,
    result_chars: view === 'done' ? 421 : 0,
  }, over);
}
function v1Job(id, view, ageS) {               // writer 1.0: no group/finished_at
  return { job_id: id, view, status: view, sessions: ['A'], duration_s: 60,
           created_at: isoAgo(ageS + 60), updated_at: isoAgo(ageS),
           exit_code: view === 'done' ? 0 : 1, result_chars: 0, error: null };
}

const listenersCount = (el, t) => (el._listeners[t] || []).length;

/* A second copy of the extension in a fresh context, sharing one
 * localStorage — i.e. "reload the tab".  Used to prove an acknowledgement
 * survives, instead of only living in the memory of the first instance. */
async function spawnSecond(src, jobs, store) {
  const { doc, rowA } = makeDoc();
  const sb = {
    document: doc, console: { warn() {}, log() {}, error() {} },
    Date, Math, JSON, Number, String, Object, Array, Set, setTimeout, clearTimeout, setInterval,
    MutationObserver: class { observe() {} disconnect() {} },
    localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
    CSS: { escape: (s) => s.replace(/["\\]/g, '\\$&') },
    location: { pathname: '/session/A' }, encodeURIComponent, decodeURIComponent,
  };
  sb.window = sb;
  sb.window.innerWidth = 1200; sb.window.innerHeight = 800; sb.window.hermesExt = null;
  sb.window.fetch = async () => ({
    ok: true, status: 200, headers: { get: () => 'application/json' },
    text: async () => JSON.stringify({ generated_at: isoAgo(1), writer_version: '1.1.0',
      done_quiet_s: 3 * 3600, notable_window_s: 24 * 3600, jobs: jobs }),
  });
  sb.__interval = () => 0;
  vm.createContext(sb);
  vm.runInContext(src.replace(/setInterval\(/g, '__interval('), sb);
  for (let i = 0; i < 6; i++) await Promise.resolve();
  await new Promise((r) => setTimeout(r, 10));
  return { doc, badge: () => rowA.querySelector('.pilamp-badge'), rowA };
}

async function run_() {
  const src = fs.readFileSync(process.argv[2], 'utf8');
  const { doc, body, row: mkRow, rowA, rowB } = makeDoc();
  const timers = [];
  let fetchMode = 'jobs';
  let payload = [];
  let realSnap = null;
  let fetchCalls = 0;
  const warns = [];
  const pollCbs = [];
  const store = {};   // shared with a second instance = "reload the tab"

  const sandbox = {
    document: doc, console: { warn: (...a) => warns.push(a.join(' ')), log() {}, error() {} },
    Date, Math, JSON, Number, String, Object, Array, Set, setTimeout, clearTimeout, setInterval,
    MutationObserver: class { observe() {} disconnect() {} },
    localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
    CSS: { escape: (s) => s.replace(/["\\]/g, '\\$&') },
    location: { pathname: '/session/A' },
    encodeURIComponent, decodeURIComponent,
  };
  sandbox.window = sandbox;
  sandbox.window.innerWidth = 1200;
  sandbox.window.innerHeight = 800;
  const jsonRes = (obj) => ({
    ok: true, status: 200, headers: { get: () => 'application/json' },
    text: async () => JSON.stringify(obj),
  });
  sandbox.window.fetch = async () => {
    fetchCalls++;
    if (fetchMode === 'throw') throw new Error('network down');
    if (fetchMode === 'http500') return { ok: false, status: 500, headers: { get: () => '' }, text: async () => '' };
    if (fetchMode === 'html') return { ok: true, status: 200, headers: { get: () => 'text/html' }, text: async () => '<html>login</html>' };
    if (fetchMode === 'nostamp') return jsonRes({ jobs: payload });   // no generated_at at all
    if (fetchMode === 'real') {   // a real writer.py snapshot, only its clock refreshed
      return jsonRes(Object.assign({}, realSnap, { generated_at: isoAgo(1) }));
    }
    return jsonRes({
      generated_at: fetchMode === 'stale' ? isoAgo(120)
        : fetchMode === 'future' ? new Date(Date.now() + 5000).toISOString().replace(/\.\d{3}Z$/, 'Z')
          : isoAgo(1),
      writer_version: '1.1.0', stall_threshold_s: 420,
      done_quiet_s: 3 * 3600, notable_window_s: 24 * 3600,
      jobs: payload,
    });
  };
  sandbox.window.hermesExt = null;
  sandbox.__interval = (cb) => { pollCbs.push(cb); return timers.length; };
  vm.createContext(sandbox);
  vm.runInContext(src.replace(/setInterval\(/g, '__interval('), sandbox);

  // start() ran at load (readyState complete); drain microtasks
  const flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); await new Promise(r => setTimeout(r, 5)); };
  const badge = (row) => row.querySelector('.pilamp-badge') || row.querySelector(':scope > .pilamp-badge');
  const chip = () => doc.getElementById('pilamp-titlebar');
  const card = () => body.querySelector('.pilamp-card');
  const cardText = () => (card() ? card().textContent : '');
  const items = (mod) => body.querySelectorAll('.pilamp-card .pilamp-item' + (mod || ''));
  const sections = () => body.querySelectorAll('.pilamp-card .pilamp-sec');
  const open = async () => { badge(rowA).fire('click'); await flush(); };
  const set = async (jobs, mode) => { payload = jobs; fetchMode = mode || 'jobs'; await pollCbs[0](); await flush(); };
  await flush();
  const results = [];
  const check = (name, cond, extra) => results.push([cond ? 'PASS' : 'FAIL', name, cond ? '' : (String(extra || ''))]);

  /* ── A. the badge answers "what is running, and how many?" ───────────── */
  await set([run('r1')]);
  check('1 running → badge counts it', badge(rowA) && badge(rowA).textContent === 'Pi: 1',
        badge(rowA) && badge(rowA).textContent);
  check('1 running → blinking "live" tone', badge(rowA) && /pilamp--live/.test(badge(rowA).className),
        badge(rowA) && badge(rowA).className);
  check('titlebar chip mirrors the counter', chip() && chip().textContent === 'Pi: 1'
        && chip().style.display !== 'none', chip() && chip().textContent);

  // the old bug: several jobs on one chat, only the first was reachable
  await set([run('r1'), run('r2', { view: 'stalled' }), term('f1', 'failed', 'recent', 7200),
             term('d1', 'done', 'quiet', 5 * 3600)]);
  check('4 jobs → badge counts RUNNING only', badge(rowA).textContent === 'Pi: 2', badge(rowA).textContent);
  check('…but a failure behind them shows as a pip',
        /pilamp--problems/.test(badge(rowA).className), badge(rowA).className);
  await open();
  check('card lists every running job', (items('--running').length + items('--stalled').length) === 2,
        String(items('--running').length + items('--stalled').length));
  check('card shows both running job ids', /r1/.test(cardText()) && /r2/.test(cardText()), cardText());
  check('running section is open by default',
        sections()[0] && /pilamp-sec--open/.test(sections()[0].className), sections()[0] && sections()[0].className);
  check('problems are a collapsed section',
        sections()[1] && !/pilamp-sec--open/.test(sections()[1].className) && /Problems/.test(cardText()), cardText());
  check('collapsed problem row is reachable in the DOM', /f1/.test(cardText()), cardText());
  check('quiet done is NOT listed, only noted',
        items('--done').length === 0 && /older than/.test(cardText()), cardText());
  check('a stalled running job says so in its row', /silent/.test(cardText()), cardText());

  /* ── B/C. no running job: static counter over the last day ───────────── */
  await set([term('f2', 'failed', 'recent', 7200), term('d2', 'done', 'recent', 3600),
             term('d3', 'done', 'quiet', 5 * 3600)]);
  check('no running → static counter over all 3', badge(rowA).textContent === 'Pi: 3', badge(rowA).textContent);
  check('an unacknowledged failure makes it "alert"', /pilamp--alert/.test(badge(rowA).className), badge(rowA).className);
  await open();
  check('finished-within-3h IS listed', items('--done').length === 1, String(items('--done').length));
  check('failure is listed with its error', items('--failed').length === 1 && /pi exited 1/.test(cardText()), cardText());
  doc.querySelector('.pilamp-card-x').fire('click');

  await set([term('d4', 'done', 'quiet', 5 * 3600), term('d5', 'done', 'quiet', 6 * 3600)]);
  check('only quiet done → counted, idle tone', badge(rowA).textContent === 'Pi: 2'
        && /pilamp--idle/.test(badge(rowA).className), badge(rowA).className + ' ' + badge(rowA).textContent);
  await open();
  check('quiet-only card lists nothing', items().length === 0, String(items().length));
  check('quiet-only card explains the number', /older than/.test(cardText()), cardText());
  doc.querySelector('.pilamp-card-x').fire('click');

  await set([]);
  check('total silence → no badge at all', !badge(rowA));
  check('total silence → chip hidden', !chip() || chip().style.display === 'none');
  await set([term('f3', 'failed', 'aged', 40 * 3600), run('r9'), term('d9', 'done', 'aged', 40 * 3600)]);
  check('aged jobs count for nothing (only r9)', badge(rowA).textContent === 'Pi: 1'
        && /pilamp--live/.test(badge(rowA).className), badge(rowA).textContent);

  /* ── back-compat: a writer-1.0 snapshot has no group / finished_at ───── */
  await set([v1Job('o1', 'done', 3600)]);
  check('v1 snapshot: done 1h ago still listed', badge(rowA).textContent === 'Pi: 1'
        && /pilamp--idle/.test(badge(rowA).className), badge(rowA).textContent);
  await open();
  check('v1 snapshot: listed in Finished', items('--done').length === 1, cardText());
  doc.querySelector('.pilamp-card-x').fire('click');
  await set([v1Job('o2', 'done', 5 * 3600)]);
  await open();
  check('v1 snapshot: done 5h ago counted but hidden', items('--done').length === 0
        && /older than/.test(cardText()), cardText());
  doc.querySelector('.pilamp-card-x').fire('click');
  await set([v1Job('o3', 'done', 30 * 3600)]);
  check('v1 snapshot: 30h ago is nothing', !badge(rowA));
  await set([v1Job('o4', 'failed', 5 * 3600)]);
  check('v1 snapshot: a failure is never quieted by time',
        badge(rowA).textContent === 'Pi: 1' && /pilamp--alert/.test(badge(rowA).className),
        badge(rowA) && (badge(rowA).textContent + ' ' + badge(rowA).className));
  await open();
  check('v1 snapshot: the failure is listed, not hidden', items('--failed').length === 1, cardText());
  doc.querySelector('.pilamp-card-x').fire('click');
  await set([v1Job('o5', 'cancelled', 5 * 3600)]);
  await open();
  check('v1 snapshot: a cancelled job ages out like a done one',
        items('--cancelled').length === 0 && /older than/.test(cardText()), cardText());
  doc.querySelector('.pilamp-card-x').fire('click');

  /* ── running → done flips the badge into a finished counter ──────────── */
  await set([run('t1')]);
  check('running: live counter', /pilamp--live/.test(badge(rowA).className) && badge(rowA).textContent === 'Pi: 1');
  await set([term('t1', 'done', 'recent', 20)]);
  check('after it finishes: static finished counter',
        /pilamp--idle/.test(badge(rowA).className) && badge(rowA).textContent === 'Pi: 1',
        badge(rowA).className + ' ' + badge(rowA).textContent);

  /* ── Acknowledge is optional manual cleanup ──────────────────────────── */
  await open();
  const ack = body.querySelector('.pilamp-card-ok');
  check('acknowledge button present', !!ack);
  ack.fire('click');
  check('ACK: badge gone synchronously (no poll in between)', !badge(rowA));
  check('ACK: chip cleared', !chip() || chip().children.length === 0);
  const callsBefore = fetchCalls;
  await set([term('t1', 'done', 'recent', 20)]);
  check('ACK: still gone after poll', !badge(rowA));
  check('poll actually ran', fetchCalls > callsBefore);

  await set([term('p1', 'failed', 'recent', 7200), term('p2', 'failed', 'recent', 3600)]);
  check('two failures → counter 2', badge(rowA).textContent === 'Pi: 2', badge(rowA).textContent);
  await open();
  const one = body.querySelectorAll('.pilamp-card .pilamp-ack');
  check('per-job clear buttons present', one.length === 2, String(one.length));
  one[0].fire('click');
  check('per-job clear decrements the counter synchronously',
        badge(rowA) && badge(rowA).textContent === 'Pi: 1', badge(rowA) && badge(rowA).textContent);

  /* ── a counter badge still knows which chat it belongs to ────────────── */
  await set([run('c1', { sessions: ['A'] }), run('c2', { sessions: ['B'] })]);
  check('two chats each count their own job',
        badge(rowA).textContent === 'Pi: 1' && badge(rowB).textContent === 'Pi: 1'
        && badge(rowA).dataset.pilampSid === 'A' && badge(rowB).dataset.pilampSid === 'B',
        badge(rowA).textContent + '/' + (badge(rowB) && badge(rowB).textContent));
  check('chip is bound to the open chat A', chip().dataset.pilampSid === 'A', chip().dataset.pilampSid);
  sandbox.location.pathname = '/session/B';            // the user switches chats
  await pollCbs[1](); await flush();                   // applyBadges, no poll
  check('chip re-binds to chat B at the same count', chip().dataset.pilampSid === 'B',
        chip().dataset.pilampSid);
  chip().querySelector('.pilamp-badge').fire('click'); await flush();
  check('chip opens chat B\'s card', /c2/.test(cardText()) && !/c1/.test(cardText()), cardText());
  doc.querySelector('.pilamp-card-x').fire('click');
  sandbox.location.pathname = '/session/A';
  // a virtualized row re-bound to another session, same count and colour
  rowB.dataset.sid = 'A';
  await pollCbs[1](); await flush();
  check('a re-bound row node re-binds its badge',
        badge(rowB) && badge(rowB).dataset.pilampSid === 'A', badge(rowB) && badge(rowB).dataset.pilampSid);
  rowB.dataset.sid = 'B';
  await pollCbs[1](); await flush();
  badge(rowB).fire('click'); await flush();
  check('and it opens the right chat again', /c2/.test(cardText()) && !/c1/.test(cardText()), cardText());
  doc.querySelector('.pilamp-card-x').fire('click');
  sandbox.location.pathname = '/session/A';
  await set([run('r9')]);

  /* ── freshness: stale / skew / transport failures ────────────────────── */
  await set([run('r9')], 'stale');
  check('stale: row badge cleared', !badge(rowA));
  const staleDot = chip() && chip().querySelector('.pilamp--stale');
  check('stale: muted dot in titlebar', !!staleDot);
  const staleWarns = warns.filter(w => /stale/.test(w)).length;
  check('stale: warned once', warns.filter(w => /stale/.test(w)).length === staleWarns);
  await set([run('r9')], 'stale');
  check('stale: warn not spammed', warns.filter(w => /stale/.test(w)).length === staleWarns, warns.join('|'));
  await set([run('r9')]);
  check('recovery: badge back', !!badge(rowA) && /pilamp--live/.test(badge(rowA).className));
  check('recovery: stale dot gone', !(chip() && chip().querySelector('.pilamp--stale')));
  await set([run('r9')], 'future');
  check('skew: badge still shown', !!badge(rowA));
  check('skew: no stale alarm ever', warns.filter(w => /stale/.test(w)).length === staleWarns,
        warns.join('|'));
  await set([run('r9')], 'html');
  await set([run('r9')], 'html');
  await set([run('r9')], 'throw');
  await set([run('r9')], 'http500');
  check('transport failures: one warn only', warns.filter(w => /poll failed/.test(w)).length === 1, warns.join('|'));
  await set([run('r9')]);
  check('after recovery: badge present', !!badge(rowA));
  const before = warns.filter(w => /stale/.test(w)).length;
  await set([run('r9')], 'nostamp');
  check('no generated_at: badge cleared', !badge(rowA));
  check('no generated_at: stale dot shown', !!(chip() && chip().querySelector('.pilamp--stale')));
  check('no generated_at: warned about stale', warns.filter(w => /stale/.test(w)).length > before);
  await set([run('r9')]);
  check('no generated_at: recovers on good snapshot', !!badge(rowA));

  /* ── listener balance across open/close cycles ───────────────────────── */
  await open();
  check('card opened', !!card());
  check('1 pointerdown listener while open', listenersCount(doc, 'pointerdown') === 1,
        String(listenersCount(doc, 'pointerdown')));
  doc.querySelector('.pilamp-card-x').fire('click');
  check('closed via ×', !card());
  check('listener removed after ×', listenersCount(doc, 'pointerdown') === 0);
  for (let i = 0; i < 3; i++) {
    badge(rowA).fire('click'); await flush();
    doc.fire('keydown', { key: 'Escape' });
  }
  check('listeners balanced after Escape cycles', listenersCount(doc, 'pointerdown') === 0,
        String(listenersCount(doc, 'pointerdown')));
  check('keydown listeners balanced', listenersCount(doc, 'keydown') === 0);
  await open();
  check('open again → exactly 1 listener', listenersCount(doc, 'pointerdown') === 1);
  doc.fire('pointerdown', { target: body });
  check('outside click closes', !card());
  check('outside click detaches', listenersCount(doc, 'pointerdown') === 0);

  /* ── optional: reader vs a REAL status.json written by writer.py ─────── */
  const snapshotPath = process.argv[3];
  if (snapshotPath) {
    realSnap = JSON.parse(fs.readFileSync(snapshotPath, 'utf8'));
    const jobs = Array.isArray(realSnap.jobs) ? realSnap.jobs : [];
    const sids = [];
    for (const j of jobs) for (const s of (j.sessions || [])) if (!sids.includes(s)) sids.push(s);
    const rows = {};
    for (const s of sids) rows[s] = mkRow('session-item', s);
    fetchMode = 'real'; payload = jobs;
    await pollCbs[0](); await flush();
    check('real snapshot has jobs to judge', jobs.length > 0, sids.join(','));
    const isActive = (j) => j.view === 'running' || j.view === 'stalled';
    for (const sid of sids) {
      const mine = jobs.filter((j) => (j.sessions || []).includes(sid));
      const act = mine.filter(isActive);
      const notable = mine.filter((j) => !isActive(j) && j.group !== 'aged');
      const n = act.length || notable.length;
      const want = n ? 'Pi: ' + n : null;
      const got = badge(rows[sid]) ? badge(rows[sid]).textContent : null;
      check('real snapshot [' + sid + ']: badge "' + want + '"', got === want, String(got));
      if (want) {
        const tone = act.length ? 'pilamp--live'
          : (notable.some((j) => j.view === 'failed' || j.view === 'interrupted') ? 'pilamp--alert' : 'pilamp--idle');
        check('real snapshot [' + sid + ']: tone ' + tone,
              badge(rows[sid]).className.indexOf(tone) >= 0, badge(rows[sid]).className);
      }
    }
    // The card of a chat that HAS work running must list every running job,
    // while a quiet (auto-hidden) finished job stays out of the list.
    for (const sid of sids) {
      const mine = jobs.filter((j) => (j.sessions || []).includes(sid));
      const act = mine.filter(isActive);
      if (!act.length || !badge(rows[sid])) continue;
      rows[sid].querySelector('.pilamp-badge').fire('click');
      await flush();
      const listed = body.querySelectorAll('.pilamp-card .pilamp-item--running').length
        + body.querySelectorAll('.pilamp-card .pilamp-item--stalled').length;
      check('real snapshot: card lists ALL ' + act.length + ' running job(s)',
            listed === act.length, String(listed));
      check('real snapshot: quiet done is not listed, only noted',
            body.querySelectorAll('.pilamp-card .pilamp-item--done').length
            === mine.filter((j) => j.group === 'recent' && (j.view === 'done' || j.view === 'cancelled')).length,
            String(body.querySelectorAll('.pilamp-card .pilamp-item--done').length));
      if (mine.some((j) => j.group === 'quiet')) {
        check('real snapshot: a quiet job is explained in the card', /older than/.test(cardText()), cardText());
      }
      const cx = body.querySelector('.pilamp-card-x');
      if (cx) cx.fire('click');
      break;
    }
    // Acknowledge on the first chat that has nothing running: a counter made
    // only of finished/failed jobs must clear completely (per-browser).
    for (const sid of sids) {
      const mine = jobs.filter((j) => (j.sessions || []).includes(sid));
      const act = mine.filter(isActive);
      const notable = mine.filter((j) => !isActive(j) && j.group !== 'aged');
      if (!act.length && notable.length && badge(rows[sid])) {
        rows[sid].querySelector('.pilamp-badge').fire('click');
        await flush();
        const btn = body.querySelector('.pilamp-card-ok');
        check('real snapshot: acknowledge offered for ' + notable.length + ' job(s)', !!btn);
        if (btn) {
          btn.fire('click');
          check('real snapshot: acknowledge clears the badge', !badge(rows[sid]),
                badge(rows[sid]) && badge(rows[sid]).textContent);
        }
        break;
      }
    }
  }

  /* ── an acknowledgement must survive reloading the tab ───────────────── */
  check('acknowledgements were persisted', /"t1"/.test(store['hermes-pilamp-dismissed'] || ''),
        Object.keys(store).join(','));
  const reloaded = await spawnSecond(src, [term('t1', 'done', 'recent', 20)], store);
  check('reload: an acknowledged job stays hidden', !reloaded.badge(),
        reloaded.badge() && reloaded.badge().textContent);
  const reloaded2 = await spawnSecond(src, [term('fresh1', 'done', 'recent', 60)], store);
  check('reload: a job nobody acknowledged still counts',
        reloaded2.badge() && reloaded2.badge().textContent === 'Pi: 1',
        reloaded2.badge() && reloaded2.badge().textContent);

  const fails = results.filter(r => r[0] === 'FAIL');
  for (const [s, n, x] of results) console.log(s.padEnd(4), n, x ? '→ ' + x : '');
  console.log('\n' + (results.length - fails.length) + '/' + results.length + ' passed');
  process.exit(fails.length ? 1 : 0);
}
run_().catch(e => { console.error('HARNESS ERROR', e); process.exit(2); });
