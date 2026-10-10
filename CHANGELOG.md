# Changelog

All notable changes to pi-hermes-bridge are documented here. Versions follow
the `pyproject.toml` package version (`pi_bridge.__version__` is kept in sync);
the `V1.x` labels in the prose and in `docs/` name the bridge protocol
revision, which is tracked independently of the package version.

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
