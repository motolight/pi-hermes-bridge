# V1.5 — WebUI completion event (bridge-local, no Hermes-core patch)

## Why (root cause, established forensically on pb-20261018T194929Z-8726e2)

v0.4.0 delivers a finished Pi job to a webui origin by running a SECOND Hermes
agent turn in a separate CLI process:
`hermes --resume <ui_session_id> chat -q <text> -Q --source tool`.
That process contends with the live WebUI backend over one session:

* if the backend holds the session lease -> instant `SESSION_NOT_OWNED`,
  no retry, the outcome is never delivered;
* if the lease is momentarily free (between turns / during backend background
  jobs) -> the CLI claims the lease, persists its user-role message, and runs
  a full model call in parallel with the backend's own in-memory session.
  When the model call outruns the delivery budget the runner SIGKILLs the
  process group: the user-like message stays in state.db as an orphan, the
  assistant turn never happens, and the backend (which never re-reads the DB)
  cannot continue it. Only a browser reload or the next user prompt absorbs
  the orphan. (2026-10-18 22:50:54 -> 22:54:52 local, agent.log.)

Both branches are wrong. A user-like `chat -q` turn is not a transport.

## Fix surface (bridge + its Hermes plugin only)

1. `pi_bridge/deliver.py` — for `origin.platform == "webui"` the runner no
   longer runs any Hermes CLI. It writes a durable delivery REQUEST (spool)
   into the job dir and records `delivery = {kind: "webui-event", attempted:
   true, ok: false, reason: "queued"}`. Messaging platforms (`hermes send`)
   are unchanged; the wake webhook is unchanged.

2. `pi_bridge/webui_event.py` (new, stdlib-only) — the spool format and pure
   logic shared by the runner and the plugin:
   * `request_path(job_id)` -> `jobs/<job_id>/webui_delivery.json`;
     atomic write (tmp + os.replace).
   * `write_request(job_id, ui_session_id, text, turn)`; one active request
     per job (a newer turn supersedes the old file content).
   * `scan_pending()` across all job dirs; returns requests with
     `status in ("pending", "injected")`.
   * `claim(req)` — atomic rename to a sibling in-flight name tagged with the
     claiming pid; the second claimer gets None (one-shot per process).
   * `build_event(req)` — the async-delegation completion event dict:
     `type="async_delegation"`, `delegation_id=f"pi-bridge-{job_id}-t{turn}"`,
     `origin_ui_session_id=session_key=<ui_session_id>`, `is_batch=False`,
     `status="completed"|"failed"`, `summary=<text>`, `error=<text or None>`,
     `goal="Pi job <job_id> (turn N): <task head>"`, numeric
     `dispatched_at/completed_at/updated_at`. Stable delegation_id = dedup
     key: re-submission for the same (job, turn) can never double-fire an
     acceptance turn (the ledger claim is the arbiter).
   * `event_path(req)` etc. must never raise into callers.

3. The bridge Hermes plugin (`plugin/__init__.py`) gains an in-process
   completion poller (daemon thread, started once per process from
   `register(ctx)`, every ~3 s, every exception caught):
   * injects ONLY in a process that can natively host the session, decided
     by the v0.4.2 process-role gate below — never by "was `gateway.run`
     ever imported" (that lazy-import check was the v0.4.1 poisoning bug);
     a process that provably holds the session's own lease
     (`active_sessions.json`, `$HERMES_HOME` honored, entry with
     `session_id == ui_session_id` and `pid == os.getpid()`) may also
     inject.  A gateway-runner process never injects → zero new behavior
     there; the request simply waits in the spool for the process that
     hosts the session.
   * injection = Hermes' OWN native background-completion pipeline:
     a durable ledger row in `async_delegations` (`INSERT OR IGNORE`,
     `state='completed'`, `delivery_state='pending'`, owner fields NULL) via
     `tools.async_delegation._transaction`, then
     `tools.process_registry.process_registry.completion_queue.put(evt)`.
     The consuming host process turns it into a real agent turn — the WebUI
     drain thread by starting a server-side wakeup turn routed by
     `origin_ui_session_id` (its documented "exact browser-tab return
     address"), the tui poller by dispatching at the idle boundary with
     `display_kind="async_delegation_complete"` (never a user-role message).
     Durable claims give at-most-once acceptance across processes, restart
     replay of pending completions gives at-least-once across crashes, and
     `delivery_state='delivered'` is the ack the poller reads back.
   * ack loop: `delivered` -> spool `status="delivered"`, job.json
     `delivery.ok=true, reason="ok"`; `dropped`/attempts-exhausted -> spool
     `status="abandoned"`, delivery `ok=false reason="native-delivery-exhausted"`
     (pi_status remains the durable source of truth); Hermes internals
     missing -> `status="unsupported"`, `reason="hermes-internal-unavailable"`,
     retried every poll tick (e.g. plugin loaded before backend import).
   * NO fallback to `chat -q` ever.

If neither gate is ever true (browser never opened), the request stays
pending forever, harmlessly: nothing is lost (job state + pi_status), the
wake webhook still fires for the operator-side acceptance run, and the
acceptance prompt itself is idempotent (pi_status first).

## Process-role gate (v0.4.2) — why "was gateway.run imported" was wrong

Root cause (incident pb-20261019T082242Z-8d9299): the WebUI backend hosts
the agent in-process, and agent code lazy-imports `gateway.run` (e.g.
`agent/relay_runtime.py` reading the gateway config during durable
compression).  From that moment `"gateway.run" in sys.modules` is true in
the REAL WebUI backend, so v0.4.1's veto turned the one legitimate host into
a permanent non-host: pending spool requests were scanned but never
injected, silently, forever.

The gate now decides by PROCESS ROLE, detected from persistent startup
signals (argv, environment, `__main__` script path) — never from lazy
imports — via `plugin._webui_process_role()` / `webui_event.process_role()`
(twin implementations pinned by `tests/test_webui_event_parity.py`):

* `gateway` — argv `-m hermes_cli.main gateway <run…>`, the `hermes gateway
  …` console script, argv `-m gateway.run`, or main script
  `…/gateway/run.py`.  NEVER injects (argv-based veto, holds even before
  `gateway.run` is imported and even while it self-holds a session lease:
  the gateway's delegation watcher terminally DROPS foreign completions).
* `webui` — env `HERMES_WEBUI_STATE_DIR`/`HERMES_WEBUI_AGENT_DIR` (set by
  the WebUI server only) or main script `…/hermes-webui/server.py|bootstrap.py`.
  Positive webui identity — role `webui`, or `api.background_process` in
  `sys.modules` (a hermes-webui-only module a gateway can never import) —
  WINS over a late incidental `gateway.run` import.
* `tui_backend` — `-m tui_gateway[.entry]` or main `…/tui_gateway/entry.py`;
  injects via its per-session notification poller.
* `unknown` (plain CLI, tests) — conservative fallback: `gateway.run`
  loaded → refuse; a backend marker loaded → allow; then the lease fallback
  (pid of the session's lease entry == own pid).

`tui_gateway.server` alone is NOT sufficient (the gateway imports it for
its hosted-room worker); the role/argv veto is evaluated first.

## Durable retry counter and budget (v0.4.2)

Every real injection attempt — including one that RAISES — increments the
on-disk `attempts` before the request returns to `pending` (v0.4.1 released
the ORIGINAL attempts on exception, so a permanently failing injector spun
forever and the exhaustion branch was unreachable).  The cap is 1200 (~1 h
at the 3 s tick), overridable with `PI_BRIDGE_MAX_INJECTION_ATTEMPTS` in
both twins; exhaustion transitions the request to `abandoned`
(`reason="injection-attempts-exhausted"`) and acks the job's delivery
`ok=false`.  Attempts live in the spool file, so restarts never reset them.

## Poller observability (v0.4.2)

Every poller process keeps `$PI_BRIDGE_HOME/health/poller-<pid>.json` (dir
0700, file 0600, atomic) current: pid, detected `role`, `may_host`, loaded
module booleans (`signals`), `started_at`/`last_tick_at`/`last_scan_at`,
`last_pending_count`, `last_gate_reason`, `pending_refused_count`, the last
injected/delivered/abandoned job ids, the last error TYPE, and counters.
It never contains task text, result text, ui_session_id or secrets.  The
file is written on startup, on meaningful change and on a 60 s heartbeat —
monotonic tick/scan/refusal counters and error timestamps are deliberately
NOT change signals (no 3 s rewrite).  Read it back with
`pi-bridge health [--json]` (read-only).  Logs go to the `pi_worker`
logger: one start line, one line per gate-reason transition, one line per
claim/inject/delivered/abandoned event, unexpected exceptions rate-limited;
idle ticks log nothing.

## Truncation (symptom B) — boundary and guard

Forensics: the pi_delegate tool-call arguments as DISPATCHED BY HERMES already
ended with `«... [truncated]` at 436 chars (state.db msg 1000049531);
job.json, turns/01.task.txt and the Pi session first user message carry those
same 436 chars byte-exact. The bridge never truncated; the task text was
already compacted upstream (Hermes history compaction rewrites old tool-call
arguments to `...[truncated]`, agent/context_compressor.py; the model copied
the compacted copy into the delegate call).

Bridge-side guard only: `pi_delegate` refuses a task whose text ends with a
compaction marker (`[truncated]` / `[truncated]»` etc., tolerating trailing
punctuation/whitespace) with `{"error": "task-text-truncated: ..."}` telling
the caller to resend the FULL text. Rejection, not silent truncation: the
bridge cannot restore what was compacted upstream, but it must never launch a
doomed job.

## Test contract

* `tests/test_webui_event.py` — spool write/read/claim/supersede/scan;
  build_event shape; delegation_id stability; deliver.py webui branch writes
  the spool and spawns NO subprocess (test fails if subprocess is called);
  kill switch `origin_delivery:false` writes nothing; messaging platforms
  still use `hermes send` (argv assertions, fake_hermes).
* `tests/test_task_fidelity.py` — long (>= 50 KB) task +
  `PI_BRIDGE_TASK_END_SENTINEL_4A91` at the very end: plugin handler ->
  submit -> job.json["task"] -> turns/01.task.txt byte-identical, sentinel
  present at the end of each; and truncation-marker rejection cases.
* Race regressions (fixtures around the spool + fake queue/ledger adapter):
  completion while session busy = request stays pending, injection is
  idempotent per (job, turn); two jobs of one session = two independent
  requests/events with distinct delegation_ids; backend restart replay =
  pending request survives scan; ack transitions (delivered/dropped/
  unsupported); the v0.4.2 gate: clean webui host injects, the SAME host
  still injects after a late `gateway.run` import (the incident
  regression), a gateway-argv process NEVER injects (all real argv shapes,
  even pre-import and even self-leased), `tui_gateway.server` alone grants
  nothing to a gateway, lease fallback intact; transient inject failure
  burns `attempts` durably and retry delivers, repeated failures exhaust
  the (env-overridable) budget into `abandoned`, attempts survive a plugin
  restart, a stuck pending spool is injected by a fresh plugin module;
  health record contents (no task/result/session-id leak), write rate
  limits, reader and CLI (`tests/test_health.py`).
* Telegram-origin delivery tests from v0.4.0 must stay green unchanged.
