# Troubleshooting

## `pi_delegate` returns "pi binary not found"

Resolution order is: `pi_bin` argument → `$PI_BRIDGE_PI_BIN` → `pi_bin` recorded
in `$PI_BRIDGE_HOME/config.json` (written by `pi-bridge install`) → `shutil.which("pi")`.
Fix: run `pi-bridge install` (or `--pi-bin /abs/path/to/pi`) so an absolute path
is recorded — this is exactly what makes jobs work in systemd units whose PATH
lacks your user bin dir.

## Job stays `queued` / never starts

- systemd user session: `systemctl --user status` ; `loginctl show-user $USER | grep Linger`
  (`loginctl enable-linger $USER` keeps units alive across logout).
- runner log: `$PI_BRIDGE_HOME/jobs/<job_id>/runner.log`; per-turn unit journal:
  `journalctl --user -u pi-bridge-<job_id>-t1`.

## `pi_status` says completed but the model "lost" the job id

`pi_list` (or `pi-bridge list`) shows recent jobs with status/cwd/task preview —
recover context without creating a duplicate delegate.

## Wake never arrives

1. `hermes plugins show pi-worker` and `hermes plugins list` — plugin enabled for
   the **gateway** profile (the woken run is a gateway run).
2. Route exists and webhook platform enabled (`docs/WAKE_SETUP.md`).
3. Secret matches between the route the wake POST goes to (static route in
   `~/.hermes/config.yaml` **or** the dynamic subscription in
   `~/.hermes/webhook_subscriptions.json`) and `$PI_BRIDGE_HOME/wake.json`
   (mismatch → gateway logs 401). The POST goes to whichever route `wake.json`
   `url` names.
4. Wake delivery log for a job: `$PI_BRIDGE_HOME/jobs/<job_id>/runner.log`
   (`wake tN:` lines). Two different states live in
   `pi-bridge status <job> --json`:
   - `.wake` — the wake POST: `{enabled, delivered, attempts, last_error}`.
     `delivered: true` means "the gateway accepted the event and started an
     agent run", nothing about the user seeing anything.
   - `.delivery` (V1.4) — the user-visible outcome: `{attempted, ok, kind,
     channel, reason, error, turn, at}`, written by the runner itself.
5. Kill switches, separately: `wake.json` `enabled: false` stops the wake POST
   only; `origin_delivery: false` stops origin delivery only. Both take effect
   on the next turn with no restart.

## Outcome never reached the WebUI conversation (V1.4: the runner delivers)

Since V1.4 the **runner** delivers the outcome along the job's origin
(`hermes --resume <ui_session_id> chat -q … -Q --source tool` for `webui`,
`hermes send -t <platform>:<chat_id>[:<thread_id>]` for messaging platforms);
the woken run does a read-only acceptance check and never sends anything.
Diagnose with `.delivery.reason`:

| reason | meaning | what to do |
|---|---|---|
| `ok` | delivered | — |
| `no-origin` / `no-delivery-channel` / `no-webui-session-id` / `no-chat-id` | nothing to deliver to (CLI-submitted or pre-V1.3 job, or `local`) | intended: log-only, the result stays in `pi_status` |
| `session_not_owned` | the WebUI session is leased by a live process | intended fail-fast; no retry, no rerouting. Close the session in its surface if you want the announcement |
| `timeout` | the resumed turn outran `delivery_timeout` (default 240 s) | a resume is a full agent turn; raise `delivery_timeout` for slow models / huge sessions |
| `hermes-binary-not-found` / `not-runnable` | the runner cannot find `hermes` | set `hermes_bin` in `wake.json` to an absolute path (transient systemd units get a clean PATH) |
| `cli-failed` / `session-not-found` / `session_not_owned`-class refusals | the CLI refused | see `.delivery.error`; `pi_status` remains the durable source |

None of these change the job's status; the result is always durable in
`pi_status` and `$PI_BRIDGE_HOME/jobs/<job_id>/final_result.md`.

## PI WEB doesn't show delegated sessions

Observability is best-effort and read-only. Check `pi-bridge web-info <job>`
(its base URL comes from `$PI_WEB_URL`, else `$PI_WEB_CONFIG`, else
`http://127.0.0.1:8504`; set `PI_BRIDGE_NO_PIWEB=1` to switch the view off
entirely):
`"available": false` with an `unreachable` reason → PI WEB is down / wrong URL
(bridge unaffected, the `pi_web` view just degrades).
`"reason": "project-not-registered"` or `"cwd-outside-allowed-paths"` → the job
cwd is outside PI WEB's `allowedPaths`; add
the path to PI WEB's own config (operator action; the bridge never edits it).
External CLI sessions may need a UI reload in some PI WEB versions — that is a
PI WEB UI limitation, not a bridge bug.

## Gateway logs show "mixed sys.modules" after a Hermes update

Run `hermes gateway restart` (Hermes' own guidance). The bridge never restarts
anything by itself.

## Tests

`cd pi-hermes-bridge && .venv/bin/python -m pytest tests/ -q` — uses a fake pi
binary, fake PI WEB and a local fake webhook receiver; no network, no real agents.
