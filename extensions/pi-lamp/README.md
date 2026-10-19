# Pi Lamp — live pi_delegate status badge in Hermes WebUI

A tiny WebUI extension that answers one question on the chat row that started
a `pi_delegate` task (the job's **origin UI session**): *is Pi working for me
right now, and on how many jobs?* — no LLM in the loop, no chat spam, no
gateway load.

A badge is a **counter**, never "the first job of the list":

| Badge | Meaning |
|---|---|
| blinking blue `Pi: N` | **N jobs running** for this chat (a running job that has gone silent is marked `silent …` inside the card); a red pip appears on the pill if failures are waiting behind them |
| static red `Pi: N` | nothing running, N finished/failed jobs of the last 24 h still unacknowledged, **and at least one of them is a failure** |
| static green `Pi: N` | nothing running, N finished jobs of the last 24 h unacknowledged, no failures |
| muted grey dot, title bar only | **stale lamp** — the snapshot itself stopped being refreshed (dead writer); job state is unknown, so no job badge is shown anywhere |
| nothing | no job for this chat, or everything inside the day window is acknowledged |

Clicking the badge (sidebar row or titlebar chip) opens a card with **every**
running job (job_id, duration, turn №/kind, last-activity age, PI WEB deep
link), then two collapsed sections — **Problems** (failed/interrupted, with
the redacted error and delivery status) and **Finished** — and a single
**Acknowledge** button. Finished jobs stop taking card space
`PI_LAMP_DONE_QUIET_S` (3 h) after they end: they stay in the badge number and
are only mentioned as "N more, hidden". A failure is never quieted by time —
acknowledging is the only way to hide one — until it leaves the 24 h window
(`PI_LAMP_NOTABLE_WINDOW_S`) and stops counting entirely. Acknowledging is
optional, immediate and per-browser.

### Who decides what counts: the writer

`status.json` carries per job `group` (`active` | `recent` | `quiet` | `aged`)
plus `finished_at` / `finished_age_s`, all computed on the **writer's** clock,
so a skewed browser clock can never move the auto-hide goalposts. The reader
uses `group` when present and falls back to `view` + `updated_at` + the
`done_quiet_s` / `notable_window_s` the payload ships — which is exactly how a
writer-1.0 snapshot (no `group`, no `finished_at`) still renders. Both fields
are additive: `view`, `status`, `sessions`, `duration_s`, … are untouched, so
other consumers of `status.json` do not change.

**Writer 1.3 (delivery honesty).** A terminal job additionally carries `hermes`
= `{state, reason, kind}` — whether HERMES itself received the completion
(`state`: `"delivered"` | `"awaiting"` | `"failed"`, or `null` when the Hermes
side is unknowable: no delivery record, a synchronous `send` /telegram
delivery, or an unrecognised shape). `"awaiting"` renders the static counter in
**amber** ("Pi done · waiting for Hermes") instead of green, and a delivery
Hermes gave up on is a problem row (red, never time-quieted,
Acknowledge-only); `null` renders exactly as writer 1.2 did.

## Architecture

```
~/.local/state/pi-bridge/jobs/*/job.json  ── read-only ──┐
~/.pi/agent/sessions/**/<ts>_<uuid>.jsonl ── mtime ──────┤  systemd timer
systemd unit / pid liveness probes        ── local ──────┤  hermes-pi-lamp-writer
PI WEB config + GET-only pi-web reads     ── local ──────┘  (.timer, 10 s)
                     │
                     ▼  atomic write, 0600
~/.hermes/webui/extensions/pi-lamp/status.json
                     │  same-origin GET /extensions/pi-lamp/status.json (no-store)
                     ▼
assets/pi-lamp.js  (browser, every 15 s; badges on .session-item/.pf-row,
                    chip in .app-titlebar; MutationObserver for virtualized rows)
```

Deliberately **no sidecar and no sidecar-proxy**: WebUI serves any file from
the managed extension dir at `/extensions/...` with `Cache-Control: no-store`
behind the normal WebUI login, so the browser reads the snapshot through the
existing authenticated static route. Nothing listens on a new port, nothing
token-related is involved, and no LLM/gateway/vLLM call exists anywhere in
this path. One writer tick costs ~0.15 s with ~20 jobs in the snapshot.

### Snapshot freshness ("stale lamp")

`status.json` is a *static* file served with `no-store`, so the HTTP status
tells the browser nothing about writer health: if the timer dies, the last
snapshot keeps being served at HTTP 200 and a finished or abandoned job would
pulse "running" forever. The extension therefore compares `generated_at`
against the browser clock and, once the snapshot is older than 3 poll
intervals + a 10 s clock-skew grace (≈ 55 s), treats every job as unknown:
all row badges are removed and a single muted, non-pulsing grey dot appears in
the title bar (only if the last good snapshot had visible jobs). A
future-dated `generated_at` is always read as fresh, so browser/writer skew
never raises the alarm. Recovery is immediate on the next good poll.

### Stall ("подвис") detection

`activity = max(mtime of the live pi session JSONL of the job's
pi_session_id, job.json, runner.log, turns/*.out|err, updated_at)`.
The pi session JSONL (`~/.pi/agent/sessions/<cwd>/<ts>_<pi_session_id>.jsonl`)
is the key signal: turn stdout (`turns/NN.out`) is only flushed at turn end,
so during a long thinking/tool turn it looks frozen while pi is very much
alive; the JSONL is appended on every assistant message / tool call. If pi's
session file can't be located the writer degrades to file mtimes and a long
silent-but-healthy turn can show a false "stalled" (alarm clears as soon as
any job file updates). Threshold: `PI_LAMP_STALL_S` (default 420 s).
"runner vanished while status=running" maps to **interrupted** (red) without
waiting for the bridge's lazy reconcile.

### PI WEB deep link

The link is assembled locally and read-only: the base URL comes from the PI
WEB config file (`PI_WEB_CONFIG`), the project id from one `GET /api/projects`
and the workspace id from one `GET /api/projects/<id>/workspaces`, both
cached (`~/.local/state/pi-lamp/piweb-cache.json`, mode 0600) and bounded by
one per-tick budget (`PI_LAMP_HTTP_BUDGET_S`, default 4 s for the whole writer
run). The writer never POSTs to PI WEB (PI WEB's own state is not ours to
mutate), imports no bridge code, never follows a redirect (the `Location` host
is one we never validated), accepts only a loopback/RFC1918/link-local **IP
literal** from the config (`localhost` is mapped to 127.0.0.1 ourselves, other
names are never resolved), and sends nothing through an egress proxy.
`PI_LAMP_PIWEB=0` disables the feature entirely. A job whose cwd PI WEB does
not know as a registered project simply has no link — the card says so.

## Files

| Path | Role |
|---|---|
| `/home/hermes/webui-extension-pi-lamp/` | source of truth (git repo) |
| `writer.py` | state scanner → status.json (stdlib only, oneshot) |
| `manage.py` | install/update/enable/disable/uninstall/status/backup |
| `assets/pi-lamp.js|css` | browser extension (additive DOM only) |
| `tests/test_writer.py`, `tests/test_registration_merge.py` | pytest suite (synthetic state only) |
| `tests/test_reader_vs_writer_js.py` | pytest: hands a real status.json to the JS harness (skipped without node) |
| `tests/js/smoke.js` | headless DOM smoke test: `node tests/js/smoke.js assets/pi-lamp.js [status.json]` |
| `~/.hermes/webui/extensions/pi-lamp/` | installed package + status.json |
| `~/.config/systemd/user/hermes-pi-lamp-writer.{service,timer}` | refresh loop |
| `~/.local/state/pi-lamp/piweb-cache.json` | pi-web deep-link cache |

## Operations

```bash
python3 manage.py status          # units, drift, status.json freshness, co-tenants
python3 manage.py update          # redeploy after editing source
python3 manage.py disable         # instant rollback (reload the WebUI tab)
systemctl --user status hermes-pi-lamp-writer.timer
```

Badge policy knobs are writer environment variables, baked into the writer
unit by `manage.py` from its own environment (default in parentheses): `PI_LAMP_STALL_S`
(420, "no activity" alarm), `PI_LAMP_DONE_QUIET_S` (10800, a finished job leaves the
card), `PI_LAMP_NOTABLE_WINDOW_S` (86400, an old terminal job stops counting), `PI_LAMP_RESULT_TTL_S`
(7 d, how long a terminal job stays in status.json at all). Example: `PI_LAMP_DONE_QUIET_S=3600 python3 manage.py update`.

Registration merges into the shared `extension-install-manifest.json` /
`extension-overrides.json` without touching the `project-folders` entries
(tests/test_registration_merge.py); the writer only ever reads bridge state.
`disable`/`uninstall` also stop the writer timer (the oneshot service is
stopped, never "disabled" — it has no `[Install]` section), `uninstall`
additionally removes the installed `status.json` (it stays fetchable via
`/extensions/...` even when unregistered). Every mutation backs both shared
files up to `~/backups/webui-pi-lamp-pre-<cmd>-<ts>` first, and a shared file
that exists but cannot be parsed aborts the whole command with no writes at
all — including `disable`, so in that emergency the repair is manual JSON
editing against those backups. The tool never replaces shared state with a
default that would silently drop co-tenant extensions.

## Tests

```bash
/home/hermes/.hermes/hermes-agent/venv/bin/python -m pytest tests -q   # 69 passed (one skips without node)
node tests/js/smoke.js assets/pi-lamp.js                              # 88 passed
node tests/js/smoke.js assets/pi-lamp.js /path/to/status.json          # 112 passed on live data (+ reader-vs-writer; the real-snapshot count follows the snapshot)
```

`test_writer.py` covers running/stalled/interrupted/done/failed views, the
live-session-JSONL stall override, terminal TTL pruning, non-webui origin
exclusion, the null-origin wildcard (`sessions: ["*"]` — a job whose origin is
null/missing/empty stays visible instead of being dropped), path redaction and
whole-placeholder truncation in `error`, fixed
terminal duration, "PI_BRIDGE_HOME is byte-for-byte unchanged after a run",
"PI WEB links are opt-in, private-IP only, never redirected, and survive a
transient outage", shape-validated / 0600 cache handling, and the 1.1 fields:
`finished_at` from the last turn (falling back to `updated_at` for a killed
runner), done→quiet after 3 h (configurable), failures never quiet, aged
beyond the day window, active-first ordering, and that every writer-1.0 key is
still present.

`tests/js/smoke.js` drives a fake DOM through: counters (one vs several
running jobs on one chat), every running job reachable in the card, collapsed
problem/finished sections, quiet jobs counted but not listed, running→finished
flipping the badge from live to a finished counter, acknowledge (whole card and
per-job) clearing synchronously, the writer-1.0 fallback for `done`, `cancelled`
and `failed`, that a counter badge stays bound to *its* chat when two chats have
the same number (title-bar chip and re-bound virtualized row), the wildcard
`"*"` bucket (unknown-origin jobs badge every chat, a chat with its own jobs
prefers them, acknowledging clears every row), that acknowledging a chat whose
own jobs run out clears the badge in ONE click instead of re-lighting it via
the wildcard fall-through (a running own job keeps the fallback hidden and
then the wildcard is left for the other chats), that an
acknowledgement survives reloading the tab (a second instance over the same
storage), stale/skew/transport failures, and exactly-balanced listeners. Given a
status.json path it also re-derives every badge (number *and* colour) from the
raw snapshot and asserts the reader agrees with the writer — that last phase is
what `tests/test_reader_vs_writer_js.py` runs.

## Limits

- Terminal jobs stay in `status.json` for `PI_LAMP_RESULT_TTL_S` (7 days, for
  other consumers) but leave the badge after `PI_LAMP_NOTABLE_WINDOW_S` (24 h)
  or on acknowledge; acknowledgements are per-browser, not server-side.
- While something is running, the badge shows the **running** count only; the
  finished/failed backlog is one click away but not in the number.
- Poll granularity ≈ 10 s (timer) + 15 s (browser) ≈ worst-case ~25 s lag.
- The private-IP guard permits any loopback/RFC1918/link-local address, so a
  PI WEB config pointing at a *different* LAN host would be reachable by
  design; today's config is this machine's own address.
- A pi process silent for > 7 min on a legitimately silent long tool call
  shows the amber alarm until the next file write (see stall detection).
- No PI WEB link for a job whose cwd is not already a registered PI WEB
  project: the writer never registers projects on PI WEB's behalf.
- Only jobs with `origin.platform == "webui"` get chat-bound badges
  (Telegram-origin jobs are invisible by design — notifications there are out
  of scope). A job whose origin is null/missing/empty belongs to no chat: the
  writer files it under session `"*"` and its badge shows on **every** chat
  row (solo-user loopback box — a live job is better seen from every window
  than not at all). Acknowledging from a chat whose own jobs run out takes
  the wildcard's terminal jobs with it — otherwise the badge would instantly
  re-light via the fall-through; the wildcard's *running* jobs are never
  ack-ed, and while a chat still has running work of its own the wildcard is
  left for the other chats.
- Polling pauses while the tab is hidden (`document.hidden`).
