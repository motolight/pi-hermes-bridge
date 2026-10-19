# Changelog

All notable changes to pi-hermes-bridge are documented here. Versions follow
the `pyproject.toml` package version (`pi_bridge.__version__` is kept in sync);
the `V1.x` labels in the prose and in `docs/` name the bridge protocol
revision, which is tracked independently of the package version.

## 0.4.2 — 2026-10-19 (protocol V1.5, unchanged — fully backward-compatible)

* **The WebUI completion poller is no longer poisoned by lazy gateway
  imports (production incident pb-20261019T082242Z-8d9299).** v0.4.1's host
  gate vetoed any process with `gateway.run` in `sys.modules` — but the
  WebUI backend hosts the agent in-process and its compression path
  lazy-imports `gateway.run` (`agent/relay_runtime.py`), so the one
  legitimate host permanently refused to inject: pending spool requests
  were scanned and silently skipped, forever. The gate now decides by
  PROCESS ROLE from persistent startup signals (argv / environment /
  `__main__` script path): a real gateway runner (`-m hermes_cli.main
  gateway run`, `hermes gateway run`, `-m gateway.run`) never injects —
  argv-based, so the veto holds even before it imports anything — while a
  positively identified WebUI backend (`HERMES_WEBUI_STATE_DIR`/
  `HERMES_WEBUI_AGENT_DIR`, `hermes-webui/server.py`, or the webui-only
  `api.background_process` module) injects even AFTER importing
  `gateway.run`; `tui_gateway.server` alone grants nothing to a gateway,
  and the session-lease fallback is preserved. Pinned by a parity table
  across both gate twins and the exact incident regression (clean
  completion, then poison, then completion still injects).
* **The durable retry counter actually burns.** An injection that raised
  was released back to `pending` with the ORIGINAL `attempts`, so a
  permanently failing injector spun forever and the exhaustion branch was
  unreachable. Every real injection attempt now persists `attempts + 1`
  even on exception; repeated failures reach `abandoned`
  (`injection-attempts-exhausted`, delivery acked `ok=false`) at the cap,
  now overridable via `PI_BRIDGE_MAX_INJECTION_ATTEMPTS` (default 1200).
  Attempts live in the spool file: a restart never resets them, and a
  pending request left by a dead backend is picked up by the next poller
  with its history intact.
* **Poller observability — no more fully-silent failures.** Each poller
  process keeps `$PI_BRIDGE_HOME/health/poller-<pid>.json` (0700/0600,
  atomic: pid, detected role, host decision, loaded-module signals, last
  tick/scan, pending count, gate reason, last injected/delivered/abandoned
  job, last error type, counters; never task/result/session-id/secret
  text), written on startup, on meaningful change and on a 60 s heartbeat
  — never every 3 s tick — plus rate-limited `pi_worker` logs (start,
  gate-reason transitions, one line per lifecycle event). New read-only
  CLI: `pi-bridge health [--json]` (reader: `pi_bridge/health.py`).
* **pi-lamp (writer 1.3): the lamp no longer lies green.** A completed
  webui-origin job is AMBER "Pi done · waiting for Hermes" while the
  native completion delivery is still queued, GREEN only once Hermes
  actually acknowledged it (`delivery.ok`), and a failed/abandoned
  delivery is a Problem (red, never time-quieted, Acknowledge-only). The
  card shows the delivery state and reason; `status.json` gains an
  additive `jobs[].hermes = {state, reason, kind}` field; old job shapes
  and messaging (`send`) deliveries render exactly as before, and
  Acknowledge stays purely browser-side.

## 0.4.1 — 2026-10-18 (protocol V1.5 — webui completion event)

* **WebUI completion no longer runs a second agent turn (root-cause fix of
  the completion race).** v0.4.0 delivered a finished job to a `webui`
  origin by running `hermes --resume <sid> chat -q <result> -Q --source
  tool` in a second process. That process contended with the live WebUI
  backend over one session: with the lease held it failed instantly
  (`SESSION_NOT_OWNED`, outcome never delivered); with the lease momentarily
  free it persisted a user-role message and raced the backend's in-memory
  history — and when the model call outran the delivery budget the runner
  killed it, leaving an orphaned user-looking message with no assistant turn
  until a browser reload. Now the runner only records a durable delivery
  request next to the job (`webui_delivery.json`), and the bridge's Hermes
  plugin — already running inside the Hermes process that hosts the session
  — injects the completion into Hermes' own native background-completion
  pipeline (async-delegation event + durable ledger + completion queue),
  whose per-session notification poller starts a real acceptance turn at the
  session's idle boundary: busy sessions back off instead of losing the
  event, restarts replay pending completions, the durable claim prevents
  duplicate acceptances, and `pi_status` remains the durable source of
  truth. Messaging-platform delivery (`hermes send`) and the wake webhook
  are unchanged. New: `pi_bridge/webui_event.py`, the spool branch of
  `pi_bridge/deliver.py`, the poller in the `pi-worker` plugin, and
  `docs/WEBUI_COMPLETION_EVENT_SPEC.md`.
* **`pi_delegate` refuses already-truncated tasks.** Forensics (job
  pb-20261018T194929Z-8726e2) showed the bridge transports task text
  byte-exact (tool-call args == `job.json["task"]` == `turns/01.task.txt`
  == the Pi session's first user message); the reported "truncation" was
  upstream Hermes history compaction (`...[truncated]`) copied verbatim by
  the delegating model into the tool-call argument. The bridge cannot
  restore text compacted upstream, so it now refuses a task whose text ends
  with a compaction marker (`task-text-truncated` error) instead of
  launching a doomed job. A fidelity regression test submits a >50 KB task
  ending in a unique sentinel and asserts byte-exact preservation across
  plugin submit → `job.json` → turn file → runner input.

## 0.4.0 — 2026-10-10 (protocol V1.4, unchanged — fully backward-compatible)

* **Runner: the Pi session dir is created before pi spawns (ENOENT fix).**
  Pi aborts at startup with ENOENT when its standard-store session directory
  for the spawn cwd (`~/.pi/agent/sessions/--<cwd-sanitized>--`) does not
  exist yet. `pi_bridge/runner.py` now mirrors pi's own
  `getDefaultSessionDirPath` naming and pre-creates the directory for every
  non-legacy job (legacy jobs pass an explicit `--session-dir`, which pi
  creates itself). New regression tests in `tests/test_session_dir.py`
  cover creation, idempotence, `$HOME` resolution and the exact sanitization
  pi performs (only `/`, `\` and `:` become `-`).
* **`extensions/pi-lamp/`: pi_delegate live status badges in the Hermes
  WebUI, now an official component of this repo.** A sidecar-free WebUI
  extension: a read-only oneshot writer (`writer.py`, systemd timer, 10 s)
  snapshots bridge job state into `status.json` served by the WebUI's own
  authenticated static route, and `assets/pi-lamp.js` renders per-chat
  counters (running / finished / failures) with a card, stall and
  "stale lamp" detection and optional local PI WEB deep links. `manage.py`
  installs/updates/rolls back the writer units and the merge-only WebUI
  registration. All paths are env-overridable with `$HOME`-based defaults;
  the writer never calls an LLM, writes nothing inside `PI_BRIDGE_HOME`, and
  redacts filesystem paths from the snapshot. See
  [`extensions/pi-lamp/README.md`](extensions/pi-lamp/README.md).
* Protocol: **V1.4, unchanged** — v0.4.0 speaks exactly the protocol of
  v0.3.0; jobs, wake payloads and `status.json` consumers of older releases
  keep working, and v0.4.0 reads state written by older releases.
* tests: 137 Python tests (134 previous + 3 session-dir regression) plus the
  pi-lamp suite (66 Python, 71 JS assertions in `tests/js/smoke.js`).
* docs: the manual static-route prompt in `docs/WAKE_SETUP.md` section 2 was a
  shortened V1.4 variant — it lacked the approval-gate circuit breaker ("do
  NOT retry or rephrase a gated call, end the turn"), `never sudo`, and
  `pi_status`/`pi_list` in the read-only allow-list, so a hand-installed route
  got a weaker prompt than `install.sh` writes. Now rule-for-rule the same as
  `hermes_setup.WAKE_PROMPT`, with a cross-reference to keep them in step.
  Docs only; no code or behaviour change.

## 0.3.0 — 2026-10-08 (protocol V1.4)

Strict origin-based delivery of the final result, executed by the runner's
code instead of by a prompt instruction.

* **The runner delivers the outcome; the wake run is read-only.** User-visible
  delivery moved out of the wake prompt and into `pi_bridge/deliver.py`: the
  runner runs the `hermes` CLI itself (argv-only, never a shell) *before* it
  wakes anyone, strictly along the job's recorded `origin` — `hermes send -t
  <platform>:<chat_id>[:<thread_id>]` for messaging platforms, `hermes
  --resume <ui_session_id> chat -q … -Q --source tool` for `webui`, and
  *nothing* for an empty/`local`/unknown origin. No default channel, no
  Telegram fallback, no retries.
  The woken Hermes run is now a read-only acceptance check. The generated
  wake prompt (`hermes_setup.WAKE_PROMPT`), the managed SOUL block and the
  `pi-routing-policy` skill forbid `hermes send` / `hermes --resume`, writes,
  service restarts and network probes and say delivery does not depend on the
  run; the `pi-worker` tool descriptions and `docs/WAKE_SETUP.md` state that
  the bridge delivers, so the woken run must not send anything itself.
* **Delivery status is reported apart from the wake notification.** Jobs now
  carry `delivery: {attempted, ok, kind, channel, reason, error, turn, at}`,
  surfaced by `pi-bridge status`/`list` --json and in the wake payload, so
  `wake.delivered` ("the gateway accepted the event, an agent run started")
  and `delivery.ok` ("the outcome reached a human channel") are finally two
  different facts. Delivery never changes a job status and never raises.
* **Bounded, killable budget.** Origin delivery runs in its own process group
  with a 240 s default budget (`delivery_timeout` /
  `PI_BRIDGE_DELIVERY_TIMEOUT`): `SIGTERM` first, then `SIGKILL` to the whole
  group. `MEDIA:` prefixes and `[[as_document]]` inside a result are
  neutralised, so a result can never turn into an attachment.
* **Two separate kill switches.** `wake.json` `enabled: false` stops the wake
  POST only; `origin_delivery: false` stops the origin delivery only. Origin
  delivery is on by default even when `wake.json` does not exist — set
  `origin_delivery: false` explicitly for a bridge that never touches a
  channel.
* **Why.** Fixes the 2026-10-07 incident (`pb-20261007T200850Z-d49a4c`, WebUI
  origin): a completed job reported `wake.delivered: true` while nothing
  reached the user, because delivery was a shell command inside a webhook
  session — the dangerous-command approval gate had nobody to answer it
  (fail-closed "Silence is not consent") and matched the outcome text
  interpolated into `hermes chat -q "…"`. Design fault, not a model error.
* Docs aligned with V1.4: `docs/WAKE_SETUP.md` (runbook retitled, the
  manual-fallback route prompt and the verification/rollback sections no
  longer assume wake-side delivery), `docs/TROUBLESHOOTING.md`,
  `docs/ROUTING_POLICY.md`, `SECURITY.md`, `README.md`.
* Tests: 134 (fake pi / fake PI WEB / fake webhook receiver / fake hermes,
  installer + uninstaller e2e, origin delivery and wake-safety cases).

## 0.2.0 — 2026-10-06

Guided one-command install/uninstall.

* `install.sh` / `uninstall.sh`: idempotent prerequisite checks (never
  auto-installing Hermes or Pi), `pi` discovery + capability probe, `pi-open-
  agents` orchestrator resolution with consent, plugin copy/enable with
  manifest verification, one controlled `hermes gateway restart` only when
  something actually changed, and no partial-install state on a hard stop.
* Managed Hermes-side setup (`pi_bridge/hermes_setup.py` +
  `pi-bridge hermes-setup`): owned SOUL.md block, routing skill, loopback
  webhook route with a fresh HMAC secret, `wake.json`, round-trip YAML editing
  that preserves foreign routes and comments, ownership state so the
  uninstaller undoes exactly what the installer owns.
* `pi-bridge web-info`: read-only PI WEB observability view of a job,
  best-effort and disableable.
* Hardening pass: loopback/url coherence, readiness gating, honest uninstall
  reporting.

## 0.1.0 — 2026-10-06 (protocol V1)

Durable Hermes → Pi delegation bridge.

* Runner with per-turn systemd units, atomic state writes + flock, restart-
  safe status reconciliation, `submit`/`status`/`feedback`/`cancel`/`list` CLI
  and the `pi-worker` Hermes plugin (`pi_delegate`, `pi_status`, `pi_feedback`,
  `pi_cancel`, `pi_list`).
* Wake channel: HMAC V2-signed `pi_bridge_turn_complete` POST to the local
  Hermes gateway with ~45 min of retries through gateway restarts (in this
  release the woken run was responsible for telling the user — superseded by
  0.3.0).
* `install.sh`/`docs/WAKE_SETUP.md` manual wake-channel runbook, routing
  policy and troubleshooting docs.
