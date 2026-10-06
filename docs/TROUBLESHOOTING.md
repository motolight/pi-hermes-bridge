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
3. Secret matches between `~/.hermes/config.yaml` route and
   `$PI_BRIDGE_HOME/wake.json` (mismatch → gateway logs 401).
4. Wake delivery log for a job: `$PI_BRIDGE_HOME/jobs/<job_id>/runner.log`
   (`wake tN:` lines); the current delivery state is in
   `pi-bridge status <job> --json` under `.wake` (delivered / attempts /
   last_error).
5. `wake.json` `enabled: false` disables the channel instantly (no restart).

## Woken run can't reply into the original WebUI conversation

Wake delivery resumes `HERMES_UI_SESSION_ID` with a short timeout. If the session
is busy or owned elsewhere, the run must NOT wait and must NOT fall back to
Telegram — the result stays durable in `pi_status`/logs and is reported on the
next user turn. That is intended behavior, not a lost job.

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
