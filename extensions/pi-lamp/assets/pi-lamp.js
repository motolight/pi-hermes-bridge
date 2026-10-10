/* Hermes WebUI extension: Pi Lamp
 *
 * Answers one question on the chat row that started a pi_delegate task (the
 * job's origin UI session): "is Pi working for me right now, and on what?"
 *
 *   >= 1 running job  -> a blinking "Pi: N" pill; the click lists EVERY
 *                        running job (id, duration, turn, PI WEB link), with
 *                        problems and finished work in collapsed sections.
 *   no running job    -> a static "Pi: N" pill counting the finished/failed
 *                        jobs of the last day that are still unacknowledged.
 *                        N == 0  -> no badge at all.
 *
 * Finished (done/cancelled) jobs stop taking card space DONE_QUIET_S after
 * they end — they stay in the number, not in the list.  A failure is never
 * quieted by time: Acknowledge is the only way to hide one, until the day
 * window passes and it stops counting entirely.
 *
 * The writer computes the group of every job on the SERVER clock (`group`,
 * `finished_at`); the thresholds below are only the fallback for an older
 * status.json, so a skewed browser clock never moves the goalposts.
 *
 * Data source: /extensions/pi-lamp/status.json (same-origin static file
 * refreshed by the local pi-lamp-writer systemd timer).  NO LLM, NO gateway,
 * NO sidecar calls — one small GET every POLL_MS while the tab is visible.
 *
 * If the snapshot itself goes stale (dead writer => the static route keeps
 * serving the last file at HTTP 200) all job badges are cleared and a single
 * muted grey "stale lamp" dot is shown in the title bar instead; see
 * STALE_AFTER_MS below.
 *
 * Design constraints (same as project-folders): additive DOM only, every
 * core global is feature-checked, failures are silent, and the extension
 * never mutates core rows.
 */
(() => {
  'use strict';
  if (window.__PILAMP_INJECTED__) return;
  window.__PILAMP_INJECTED__ = true;

  const EXT_ID = 'pi-lamp';
  const POLL_MS = 15000;
  const STATUS_URL = 'extensions/pi-lamp/status.json';
  const DISMISS_KEY = 'hermes-pilamp-dismissed';

  /* Fallback time policy, used only when a snapshot has no per-job `group`
   * (writer < 1.1).  A newer snapshot ships `done_quiet_s` /
   * `notable_window_s`, which override these. */
  const DONE_QUIET_S = 3 * 3600;        // finished: out of the card after this
  const NOTABLE_WINDOW_S = 24 * 3600;   // older terminal jobs stop counting

  const ACTIVE_VIEWS = { running: 1, stalled: 1 };
  // "it ended without a problem" — ages out of the card on its own:
  const QUIET_VIEWS = { done: 1, cancelled: 1 };
  // any other terminal view is a problem: only Acknowledge hides it.
  const GROUPS = { active: 1, recent: 1, quiet: 1, aged: 1 };
  const ACTIVE_RANK = { stalled: 0, running: 1 };

  const VIEW_LABEL = {
    running: 'Pi works…', stalled: 'Pi silent (stalled?)', done: 'Pi done',
    failed: 'Pi failed', cancelled: 'Pi cancelled', interrupted: 'Pi interrupted',
  };

  /* Freshness of the snapshot.
   *
   * status.json is served by a *static* route with cache: no-store, so the
   * HTTP status tells us nothing about writer health: if the pi-lamp-writer
   * systemd timer dies, the last file keeps being served at HTTP 200 forever
   * and a finished/abandoned job would keep a pulsing "running" badge.
   * So we compare generated_at (UTC ISO, whole seconds) against the browser
   * clock: once the snapshot is older than ~3 poll intervals (+ a clock-skew
   * grace), every job is treated as "unknown/stale" — no running/stalled
   * badges, only one muted non-pulsing "stale lamp" dot in the title bar.
   * A future-dated generated_at (browser clock a few seconds behind the
   * writer) is clamped to "fresh", so small skew never raises the alarm —
   * not even once. Recovery is immediate: the next good poll clears stale.
   */
  const STALE_AFTER_MS = POLL_MS * 3;   // 45s without a newer snapshot
  const CLOCK_GRACE_MS = 10000;         // tolerated browser/writer clock skew

  const state = {
    bySession: new Map(), // session_id -> bucket {active, problem, done, quiet, …}
    dismissed: {},        // job_id -> ts (terminal badges the user cleared)
    lastGenMs: null,      // parsed generated_at (epoch ms) or null
    knownJobs: 0,         // jobs visible in the last good snapshot
    quietS: DONE_QUIET_S,
    windowS: NOTABLE_WINDOW_S,
    failStreak: 0,
    failLogged: false,    // log at most once per failure streak
    staleLogged: false,   // log at most once per stale streak
  };

  /* ── storage (extension-owned, with localStorage fallback) ─────────── */
  let extHandle = null;
  try {
    if (window.hermesExt && typeof window.hermesExt.register === 'function') {
      extHandle = window.hermesExt.register(EXT_ID);
    }
  } catch (_) { /* optional */ }

  function loadDismissed() {
    try {
      if (extHandle && extHandle.storage && typeof extHandle.storage.get === 'function') {
        const v = extHandle.storage.get('dismissed');
        if (v && typeof v === 'object') return v;
      }
    } catch (_) {}
    try {
      const raw = localStorage.getItem(DISMISS_KEY);
      const p = raw ? JSON.parse(raw) : null;
      if (p && typeof p === 'object') return p;
    } catch (_) {}
    return {};
  }
  function saveDismissed() {
    // prune to the 300 newest entries; entries are job_id -> epoch ms
    const keys = Object.keys(state.dismissed);
    if (keys.length > 300) {
      keys.sort((a, b) => (state.dismissed[b] || 0) - (state.dismissed[a] || 0));
      for (const k of keys.slice(300)) delete state.dismissed[k];
    }
    try {
      if (extHandle && extHandle.storage && typeof extHandle.storage.set === 'function') {
        extHandle.storage.set('dismissed', state.dismissed);
        return;
      }
    } catch (_) {}
    try { localStorage.setItem(DISMISS_KEY, JSON.stringify(state.dismissed)); } catch (_) {}
  }
  function dismiss(jobs) {
    const now = Date.now();
    const ids = new Set();
    for (const j of jobs) { if (j && j.job_id) { state.dismissed[j.job_id] = now; ids.add(j.job_id); } }
    if (!ids.size) return;
    saveDismissed();
    // A badge is a counter, so it cannot simply be removed from the DOM: the
    // live buckets must lose the job on THIS tick (re-labelled, or dropped
    // when nothing is left) and only then be re-rendered — not on the next
    // poll tick.
    for (const [sid, b] of Array.from(state.bySession)) {
      for (const k of ['active', 'problem', 'done', 'quiet']) {
        b[k] = b[k].filter((j) => !ids.has(j.job_id));
      }
      if (!b.active.length && !b.problem.length && !b.done.length && !b.quiet.length) {
        state.bySession.delete(sid);
      } else {
        finishBucket(b);
      }
    }
    // The stale-lamp dot only appears when the last good snapshot had
    // something visible, so this count must follow the acknowledgement too.
    state.knownJobs = countVisibleJobs();
    applyBadges();
  }

  function countVisibleJobs() {
    const ids = new Set();
    for (const b of state.bySession.values()) {
      for (const k of ['active', 'problem', 'done', 'quiet']) {
        for (const j of b[k]) ids.add(j.job_id);
      }
    }
    return ids.size;
  }

  /* ── helpers ─────────────────────────────────────────────────────────── */
  function activeSessionId() {
    try {
      const s = (typeof S !== 'undefined' && S && S.session && S.session.session_id) || null;
      if (s) return s;
    } catch (_) {}
    try {
      const m = location.pathname.match(/\/session\/([^/?#]+)/);
      if (m) return decodeURIComponent(m[1]);
    } catch (_) {}
    return null;
  }

  function fmtDur(sec) {
    sec = Math.max(0, Math.round(Number(sec) || 0));
    if (sec < 60) return sec + 's';
    const m = Math.floor(sec / 60);
    if (m < 60) return m + 'm ' + (sec % 60) + 's';
    const h = Math.floor(m / 60);
    return h + 'h ' + (m % 60) + 'm';
  }

  function parseIso(s) {
    if (typeof s !== 'string' || !s) return null;
    let t = s.trim();
    if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(t)) t += 'Z';
    const ms = Date.parse(t.replace(' ', 'T'));
    return Number.isFinite(ms) ? ms / 1000 : null;
  }

  function parseGenMs(genIso) {
    const s = parseIso(genIso);
    return s == null ? null : s * 1000;
  }

  function isStale() {
    // A snapshot we could not timestamp at all is *unknown*, not fresh:
    // without this a malformed/foreign status.json would keep a pulsing
    // "running" badge forever after the writer died.
    if (state.lastGenMs == null) return state.knownJobs > 0;
    const age = Date.now() - state.lastGenMs;
    if (age <= 0) return false;                  // future-dated → clock skew, never stale
    return age > STALE_AFTER_MS + CLOCK_GRACE_MS;
  }

  /* Terminal age of a job on the browser clock — fallback path only. */
  function terminalAgeS(j) {
    const t = parseIso(j.finished_at) || parseIso(j.updated_at);
    return t == null ? null : (Date.now() / 1000) - t;
  }

  /* active | recent | quiet | aged — writer's answer if present, else ours. */
  function groupOf(j) {
    if (ACTIVE_VIEWS[j.view]) return 'active';
    if (typeof j.group === 'string' && GROUPS[j.group]) return j.group;
    const age = terminalAgeS(j);
    if (age == null) return 'recent';            // unknown age: show, don't hide
    if (age > state.windowS) return 'aged';
    if (QUIET_VIEWS[j.view] && age > state.quietS) return 'quiet';
    return 'recent';
  }

  /* ── polling ─────────────────────────────────────────────────────────── */
  function emptyBucket() {
    return { active: [], problem: [], done: [], quiet: [], count: 0, tone: 'idle', sig: '' };
  }

  function bucketize(data) {
    const bySession = new Map();
    const seen = new Set();
    for (const j of data.jobs) {
      if (!j || !j.job_id || !Array.isArray(j.sessions)) continue;
      const g = groupOf(j);
      if (g === 'aged') continue;                       // not running, not news
      if (g !== 'active' && state.dismissed[j.job_id]) continue;  // acknowledged
      for (const sid of j.sessions) {
        let b = bySession.get(sid);
        if (!b) { b = emptyBucket(); bySession.set(sid, b); }
        if (g === 'active') b.active.push(j);
        else if (QUIET_VIEWS[j.view]) (g === 'quiet' ? b.quiet : b.done).push(j);
        else b.problem.push(j);
        seen.add(j.job_id);
      }
    }
    for (const b of bySession.values()) finishBucket(b);
    return { bySession, jobs: seen.size };
  }

  const byNewest = (a, b) => String(b.finished_at || b.updated_at || '')
    .localeCompare(String(a.finished_at || a.updated_at || ''));

  function finishBucket(b) {
    b.active.sort((a, c) => ((ACTIVE_RANK[a.view] ?? 9) - (ACTIVE_RANK[c.view] ?? 9))
      || String(c.updated_at || '').localeCompare(String(a.updated_at || '')));
    b.problem.sort(byNewest);
    b.done.sort(byNewest);
    b.quiet.sort(byNewest);
    const terminal = b.problem.length + b.done.length + b.quiet.length;
    const n = b.active.length || terminal;
    b.count = n;
    b.terminal = terminal;
    b.tone = b.active.length ? 'live' : (b.problem.length ? 'alert' : 'idle');
    b.problems = b.problem.length;
    // A card is rebuilt from live state on every click, so the DOM only needs
    // to know when what the user SEES changes: the number, the colour, and a
    // red pip when failures are waiting behind running work.
    b.sig = b.tone + ':' + n + (b.active.length && b.problems ? '+' + b.problems : '');
  }

  async function poll() {
    let data = null, why = '';
    try {
      const r = await fetch(STATUS_URL + '?_=' + Date.now(), {
        credentials: 'same-origin', cache: 'no-store',
      });
      if (r.ok) {
        // A 302 to a login page is followed transparently by fetch, so the
        // body can be HTML (or the response opaque) even when r.ok is true.
        // Read as text and parse defensively — never an uncaught rejection.
        const text = await r.text();
        try { data = JSON.parse(text); } catch (_) {
          why = 'non-JSON body (' + (r.headers.get('content-type') || 'unknown type') + ')';
        }
      } else {
        why = 'HTTP ' + r.status;
      }
    } catch (e) {
      why = 'fetch threw: ' + ((e && e.message) || e);
    }

    if (!data || !Array.isArray(data.jobs)) {
      // Unreachable / unparseable: keep the last map, but the freshness rule
      // in isStale() (lastGenMs getting older) will clear the badges too.
      state.failStreak++;
      if (state.failStreak === 1 && !state.failLogged) {
        state.failLogged = true;
        try { console.warn('[pi-lamp] status poll failed (' + why + '); will retry quietly'); } catch (_) {}
      }
      // if the writer stays unreachable for a long time, drop the map as well
      // (but NOT knownJobs — it is what keeps the "pi-lamp is broken" dot on)
      if (state.failStreak >= 20) { state.bySession.clear(); }
      applyBadges();
      return;
    }
    state.failStreak = 0;
    state.failLogged = false;
    // server-side thresholds, so the fallback above agrees with the writer
    if (Number(data.done_quiet_s) > 0) state.quietS = Number(data.done_quiet_s);
    if (Number(data.notable_window_s) > 0) state.windowS = Number(data.notable_window_s);

    const { bySession, jobs } = bucketize(data);
    state.bySession = bySession;
    state.knownJobs = jobs;
    state.lastGenMs = parseGenMs(data.generated_at);
    applyBadges();   // a good snapshot clears the stale state immediately
  }

  /* ── DOM: badges ─────────────────────────────────────────────────────── */
  // A badge is a COUNTER, not a job: it is rebuilt only when the number or
  // the tone changes, and always reads the current bucket when clicked.
  function badgeFor(ref) {
    const b = document.createElement('span');
    const live = ref.info.active.length > 0;
    b.className = 'pilamp-badge pilamp--' + ref.info.tone
      + (live && ref.info.problems ? ' pilamp--problems' : '');
    b.textContent = 'Pi: ' + ref.info.count;
    b.dataset.pilampSig = ref.info.sig;
    b.dataset.pilampSid = ref.sid == null ? '' : String(ref.sid);
    b.setAttribute('role', 'button');
    b.setAttribute('tabindex', '0');
    let msg = live
      ? ref.info.count + ' Pi job(s) running — click for the list'
      : ref.info.count + ' finished/failed Pi job(s) waiting for you';
    if (live && ref.info.problems) {
      msg += ' · ' + ref.info.problems + ' unacknowledged failure(s) behind them';
    }
    b.title = msg;
    b.setAttribute('aria-label', msg);
    const open = (ev) => { ev.preventDefault(); ev.stopPropagation(); openCard({ sid: ref.sid, anchor: b }); };
    b.addEventListener('click', open);
    b.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') open(ev); });
    return b;
  }

  // Re-render only when what the user SEES changes — including WHICH session
  // the badge belongs to, because a virtualized row (and the shared title-bar
  // chip) can keep the same count while being re-bound to another chat.
  function sameBadge(el, ref) {
    const sid = ref.sid == null ? '' : String(ref.sid);
    return !!el && el.dataset.pilampSig === ref.info.sig && el.dataset.pilampSid === sid;
  }

  function applyInto(container, ref) {
    if (!container) return;
    const old = container.querySelector(':scope > .pilamp-badge');
    if (sameBadge(old, ref)) return;
    if (old) old.remove();
    container.appendChild(badgeFor(ref));
  }

  // Bucket for a session, resolved *at render/click time*.
  function bucketFor(sid) { return sid ? (state.bySession.get(sid) || null) : null; }

  // Muted, non-interactive dot shown when the snapshot has gone stale.
  function staleBadgeEl() {
    const b = document.createElement('span');
    b.className = 'pilamp-badge pilamp--stale';
    b.dataset.pilampView = 'stale';
    b.setAttribute('role', 'img');
    const msg = 'Pi lamp stale: status snapshot is older than '
      + Math.round((STALE_AFTER_MS + CLOCK_GRACE_MS) / 1000) + 's — job state unknown';
    b.title = msg;
    b.setAttribute('aria-label', msg);
    // It is a .pilamp-badge, so the card's outside-click handler ignores it;
    // clicking it should still close an open card.
    b.addEventListener('click', () => closeCard());
    return b;
  }

  function applyBadges() {
    const stale = isStale();
    if (stale) {
      if (!state.staleLogged) {
        state.staleLogged = true;
        try { console.warn('[pi-lamp] status.json snapshot is stale; clearing job badges until the writer updates again'); } catch (_) {}
      }
    } else {
      state.staleLogged = false;
    }

    const rows = document.querySelectorAll(
      '#panelChat .session-item[data-sid], #panelChat .pf-row[data-sid], #panelChat .session-child-session[data-sid]');
    for (const row of rows) {
      const info = stale ? null : bucketFor(row.dataset.sid);
      const host = row.querySelector('.session-title-row') || row.querySelector('.pf-row-title') || row;
      if (!info) {
        // Clear every badge anywhere inside the row, not only the one under the
        // currently chosen host: a partial re-render can introduce a new
        // .session-title-row and orphan a badge we appended to the row itself.
        for (const b of row.querySelectorAll('.pilamp-badge')) b.remove();
        continue;
      }
      applyInto(host, { sid: row.dataset.sid, info });
      const keep = host.querySelector(':scope > .pilamp-badge');
      for (const b of row.querySelectorAll('.pilamp-badge')) { if (b !== keep) b.remove(); }
    }
    // title-bar chip for the open chat (or one global "stale lamp" dot)
    try {
      const inner = document.querySelector('.app-titlebar-inner');
      if (!inner) return;
      let chip = document.getElementById('pilamp-titlebar');
      if (!chip) {
        chip = document.createElement('span');
        chip.id = 'pilamp-titlebar';
        chip.style.display = 'none';
        inner.appendChild(chip);
      }
      const hideChip = () => {
        chip.style.display = 'none';
        chip.textContent = '';
        chip.dataset.pilampSig = '';
        chip.dataset.pilampSid = '';
      };
      if (stale) {
        // Nothing is knowable: drop every badge and show a single muted dot,
        // but only if the last good snapshot actually had visible jobs.
        if (!state.knownJobs) { hideChip(); return; }
        if (chip.dataset.pilampSig !== 'stale') {
          hideChip();
          chip.appendChild(staleBadgeEl());
          chip.dataset.pilampSig = 'stale';
        }
        chip.style.display = '';
        return;
      }
      const sid = activeSessionId();
      const info = bucketFor(sid);
      if (!info) { hideChip(); return; }
      const sidKey = sid == null ? '' : String(sid);
      if (chip.dataset.pilampSig !== info.sig || chip.dataset.pilampSid !== sidKey) {
        chip.dataset.pilampSig = info.sig;
        chip.dataset.pilampSid = sidKey;
        chip.textContent = '';
        chip.appendChild(badgeFor({ sid, info }));
      }
      chip.style.display = '';
    } catch (_) {}
  }

  /* ── DOM: detail card ────────────────────────────────────────────────── */
  let cardEl = null;
  let docCloseHandler = null;   // the ONE document-level pointerdown listener
  let attachTimer = 0;          // pending deferred attach (must be cancellable)

  // Strictly balanced: exactly one document listener exists while exactly one
  // card is open. Detaching before (re-)adding means close-via-×, close-via-
  // Escape and close-via-outside-click all release it, so open/close cycles
  // can no longer stack a permanent listener each time.
  function detachDocClose() {
    if (attachTimer) { clearTimeout(attachTimer); attachTimer = 0; }
    if (!docCloseHandler) return;
    document.removeEventListener('pointerdown', docCloseHandler, true);
    docCloseHandler = null;
  }

  function closeCard() {
    detachDocClose();
    if (cardEl) { cardEl.remove(); cardEl = null; }
    document.removeEventListener('keydown', onCardKey);
  }
  function onCardKey(ev) { if (ev.key === 'Escape') closeCard(); }

  // After an acknowledgement, keep the card open and show what is left (the
  // anchor badge may well be detached by now, so the position is preserved);
  // close it only when this chat has nothing left to show.
  function refreshCard(ref) {
    const info = bucketFor(ref.sid);
    if (!info || (!info.active.length && !info.terminal)) { closeCard(); return; }
    openCard(ref);
  }

  function el(cls, text) {
    const e = document.createElement('span');
    if (cls) e.className = cls;
    if (text != null) e.textContent = String(text);
    return e;
  }

  // A collapsible section: header toggles, body starts collapsed except for
  // the running list — that one is the whole point of the badge.  Which
  // sections the user opened is remembered by key, so clearing one job (which
  // rebuilds the card) does not collapse what they had just opened.
  const sectionOpen = {};

  function section(key, title, tone, open) {
    const isOpenNow = (key in sectionOpen) ? sectionOpen[key] : !!open;
    const box = document.createElement('div');
    box.className = 'pilamp-sec' + (isOpenNow ? ' pilamp-sec--open' : '');
    const h = document.createElement('button');
    h.type = 'button';
    h.className = 'pilamp-sec-head pilamp-text--' + tone;
    const label = () => { h.textContent = (isOpenNow ? '▾ ' : '▸ ') + title; };
    label();
    h.onclick = () => {
      sectionOpen[key] = !(key in sectionOpen ? sectionOpen[key] : !!open);
      const now = sectionOpen[key];
      box.className = 'pilamp-sec' + (now ? ' pilamp-sec--open' : '');
      h.textContent = (now ? '▾ ' : '▸ ') + title;
    };
    const body = document.createElement('div');
    body.className = 'pilamp-sec-body';
    box.appendChild(h); box.appendChild(body);
    return { box, body };
  }

  function itemBox(cls) {
    const d = document.createElement('div');
    d.className = 'pilamp-item' + (cls ? ' ' + cls : '');
    return d;
  }

  function ackButton(j, onDone) {
    const btn = document.createElement('button');
    btn.className = 'pilamp-ack'; btn.type = 'button'; btn.textContent = 'clear';
    btn.title = 'Acknowledge — hide this badge (per-browser)';
    btn.onclick = (ev) => { ev.stopPropagation(); onDone(); };
    return btn;
  }

  function runningItem(s, j) {
    const it = itemBox('pilamp-item--' + j.view);
    const l1 = el('pilamp-item-main');
    l1.appendChild(el('pilamp-item-id', j.job_id));
    if (j.view === 'stalled') l1.appendChild(el('pilamp-warn', ' · silent ' + fmtDur(j.activity_age_s)));
    else l1.appendChild(el('pilamp-dim', ' · ' + VIEW_LABEL[j.view]));
    it.appendChild(l1);
    const bits = [fmtDur(j.duration_s) + ' elapsed'];
    if (j.turn_n != null) bits.push('turn #' + j.turn_n + (j.turn_kind ? ' · ' + j.turn_kind : '')
      + ' of ' + (j.turns_total || j.turn_n));
    if (j.result_chars) bits.push(j.result_chars + ' chars so far');
    it.appendChild(el('pilamp-item-sub', bits.join(' · ')));
    const link = piwebRow(j);
    if (link) it.appendChild(link);
    s.body.appendChild(it);
  }

  function piwebRow(j) {
    if (!(j.pi_web && j.pi_web.url && /^https?:\/\//.test(j.pi_web.url))) return null;
    const w = document.createElement('div');
    w.className = 'pilamp-card-row';
    const a = document.createElement('a');
    a.className = 'pilamp-card-link';
    a.href = j.pi_web.url; a.target = '_blank'; a.rel = 'noopener';
    a.textContent = 'Open in PI WEB' + (j.pi_web.project_name ? ' · ' + j.pi_web.project_name : '');
    w.appendChild(a);
    return w;
  }

  function terminalItem(s, j, kind, ref) {
    const it = itemBox('pilamp-item--' + j.view);
    const l1 = el('pilamp-item-main');
    l1.appendChild(el('pilamp-item-id', j.job_id));
    l1.appendChild(el(kind === 'problem' ? 'pilamp-warn' : 'pilamp-dim',
      ' · ' + (VIEW_LABEL[j.view] || j.view)));
    l1.appendChild(ackButton(j, () => { dismiss([j]); refreshCard(ref); }));
    it.appendChild(l1);
    const age = (j.finished_age_s != null) ? fmtDur(j.finished_age_s) + ' ago'
      : (j.updated_at ? 'updated ' + j.updated_at : '');
    const bits = [];
    if (age) bits.push(age);
    bits.push('took ' + fmtDur(j.duration_s));
    if (j.exit_code !== null && j.exit_code !== undefined) bits.push('exit ' + j.exit_code);
    if (j.status) bits.push(j.status);
    if (j.result_chars) bits.push(j.result_chars + ' chars');
    it.appendChild(el('pilamp-item-sub', bits.join(' · ')));
    if (j.delivery && !j.delivery.ok && j.delivery.attempted) {
      it.appendChild(el('pilamp-item-sub', 'report NOT delivered ('
        + (j.delivery.reason || 'error') + ')'));
    }
    if (j.error) it.appendChild(el('pilamp-err', String(j.error).slice(0, 200)));
    const link = piwebRow(j);
    if (link) it.appendChild(link);
    s.body.appendChild(it);
  }

  function openCard(ref) {
    // Always render the CURRENT bucket: a badge is a counter, and a card built
    // from the snapshot that happened to be on screen when it was clicked
    // would happily show a job that is gone by now.
    const info = bucketFor(ref.sid);
    if (!info) { closeCard(); return; }
    const prev = cardEl ? { left: cardEl.style.left, top: cardEl.style.top } : null;
    closeCard();
    const c = document.createElement('div');
    c.className = 'pilamp-card';
    c.setAttribute('role', 'dialog');
    c.setAttribute('aria-label', 'Pi jobs');

    const head = document.createElement('div');
    head.className = 'pilamp-card-head';
    const headCls = info.active.length ? 'live' : (info.problem.length ? 'alert' : 'idle');
    const title = info.active.length
      ? 'Pi is working — ' + info.active.length
      : (info.problem.length ? 'Needs attention — ' + info.count : 'Finished work — ' + info.count);
    const ttl = el('pilamp-card-title pilamp-text--' + headCls, title);
    head.appendChild(ttl);
    const x = document.createElement('button');
    x.className = 'pilamp-card-x'; x.type = 'button'; x.textContent = '×';
    x.title = 'Close';
    x.onclick = closeCard;
    head.appendChild(x);
    c.appendChild(head);

    const bodyEl = document.createElement('div');
    bodyEl.className = 'pilamp-card-body';
    c.appendChild(bodyEl);

    if (info.active.length) {
      const s = section('running', 'Running — ' + info.active.length, 'live', true);
      for (const j of info.active) runningItem(s, j);
      bodyEl.appendChild(s.box);
    }
    if (info.problem.length) {
      const s = section('problem', 'Problems — ' + info.problem.length, 'alert', false);
      for (const j of info.problem) terminalItem(s, j, 'problem', ref);
      bodyEl.appendChild(s.box);
    }
    if (info.done.length) {
      const s = section('done', 'Finished — ' + info.done.length, 'idle', false);
      for (const j of info.done) terminalItem(s, j, 'done', ref);
      bodyEl.appendChild(s.box);
    }
    const cleared = info.quiet.length;
    if (cleared) {
      bodyEl.appendChild(el('pilamp-note',
        cleared + ' finished ' + (cleared === 1 ? 'job is' : 'jobs are') + ' older than '
        + fmtDur(state.quietS) + ' — counted in the badge, hidden from this list.'));
    }
    if (!info.active.length && !info.problem.length && !info.done.length && !cleared) {
      bodyEl.appendChild(el('pilamp-note', 'Nothing running.'));
    }

    const all = info.problem.concat(info.done, info.quiet);
    if (all.length) {
      const btn = document.createElement('button');
      btn.className = 'pilamp-card-ok'; btn.type = 'button';
      btn.textContent = 'Acknowledge ' + all.length
        + ' finished / failed ' + (all.length === 1 ? 'job' : 'jobs');
      btn.title = 'Optional: clears the badge now. Finished jobs also stop being listed on their own after '
        + fmtDur(state.quietS) + '.';
      btn.onclick = () => { dismiss(all); refreshCard(ref); };
      c.appendChild(btn);
    }

    document.body.appendChild(c);
    cardEl = c;
    // Position near the badge — or keep the place we already had, because a
    // rebuild after "clear" must not jump under the cursor.
    if (prev && prev.left) {
      c.style.left = prev.left;
      c.style.top = prev.top;
    } else {
      const anchor = ref.anchor || document.body;
      const r = anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : { top: 80, left: 80, bottom: 20 };
      const cw = 340, ch = c.offsetHeight || 260;
      const left = Math.max(8, Math.min(window.innerWidth - cw - 8, r.left - 4));
      let top = r.bottom + 6;
      if (top + ch > window.innerHeight - 8) top = Math.max(8, r.top - ch - 6);
      c.style.left = left + 'px';
      c.style.top = top + 'px';
    }
    // onCardKey is a stable module-level function: add/remove stays balanced.
    document.removeEventListener('keydown', onCardKey);
    document.addEventListener('keydown', onCardKey);
    // Defer one turn so the pointerdown that opened the card cannot close it.
    detachDocClose();
    attachTimer = setTimeout(() => {
      attachTimer = 0;
      if (!cardEl) return;
      const onDoc = (ev) => {
        if (!cardEl) { detachDocClose(); return; }
        if (cardEl.contains(ev.target)) return;
        const t = ev.target;
        // Clicking another badge hands over to openCard() via its own handler.
        if (t && typeof t.closest === 'function' && t.closest('.pilamp-badge')) return;
        detachDocClose();
        closeCard();
      };
      docCloseHandler = onDoc;
      document.addEventListener('pointerdown', onDoc, true);
    }, 0);
  }

  /* ── bootstrap ───────────────────────────────────────────────────────── */
  function start() {
    state.dismissed = loadDismissed();
    poll();
    setInterval(() => {
      try { if (document.hidden) return; } catch (_) {}
      poll();
    }, POLL_MS);
    // sidebar rows are virtualized/re-rendered by core and by project-folders:
    // re-apply badges on DOM changes (cheap: no network, memory map only)
    let queued = 0;
    const obs = new MutationObserver(() => {
      if (queued) return;
      queued = setTimeout(() => { queued = 0; applyBadges(); }, 300);
    });
    const target = document.getElementById('panelChat') || document.body;
    obs.observe(target, { childList: true, subtree: true });
    setInterval(applyBadges, 5000); // belt & braces if observers miss a swap
    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { try { start(); } catch (_) {} });
  } else {
    try { start(); } catch (_) {}
  }
})();
