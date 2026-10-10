# Changelog

All notable changes to pi-hermes-bridge are documented here. Versions follow
the `pyproject.toml` package version (`pi_bridge.__version__` is kept in sync);
the `V1.x` labels in the prose and in `docs/` refer to the bridge protocol
revision, which is one notch ahead of the package version.

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
  The woken Hermes run is now a read-only acceptance check: its generated
  prompt (`hermes_setup.WAKE_PROMPT`, the SOUL block, `pi-routing-policy`, the
  `pi-worker` tool descriptions and `docs/WAKE_SETUP.md`) forbids `hermes
  send` / `hermes --resume`, writes, service restarts and network probes, and
  states that delivery does not depend on it.
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
