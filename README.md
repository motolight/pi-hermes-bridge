# pi-hermes-bridge

**Let Hermes manage the job while Pi does the heavy coding work in the background.**

If Hermes is your main assistant, this bridge lets it hand substantial coding,
DevOps, scripting and infrastructure work to a **Pi orchestrator**, then wake back
up when Pi is done, review the result independently, and continue the **same Pi
session** if something still needs fixing.

## Why use this?

- **Potentially save money on hosted models.** Keep Hermes on the stronger or more
  expensive model you prefer for planning, conversation and review, while Pi does
  implementation on a cheaper model. The bridge does not require both agents to
  use the same provider or model.
- **Potentially speed up local-model workflows.** Long implementation jobs run in
  the background instead of blocking the Hermes conversation, and Pi can use its
  own explorer / worker / reviewer subagents in parallel or as a pipeline.
- **Keep Hermes as the manager.** Hermes owns the user conversation, context,
  constraints and acceptance criteria; Pi owns implementation details.
- **Keep an independent acceptance loop.** When Pi finishes, Hermes wakes up,
  checks the actual result, and can send concrete feedback back into the same Pi
  session for another repair turn.
- **Stop babysitting long jobs.** Jobs are durable, survive gateway interruptions,
  can be recovered with `pi_list`, and do not require the user to keep asking
  "is it done yet?"
- **Use different models for different roles.** This is especially useful if you
  self-host models and want to reserve the strongest model for orchestration, or
  if you pay per token and want implementation work to run somewhere cheaper.

In short:

**Hermes = manager / user interface / acceptance reviewer**  
**Pi = implementation team**  
**pi-hermes-bridge = the glue between them**

It does **not** magically make every workload faster or cheaper: that depends on
your model choices, hardware, prompts and subagent setup. The point is that it
makes those trade-offs possible without turning the user into the message bus
between two agents.

## How it works

```
User ──▶ Hermes ──▶ pi_delegate ──▶ bridge runner (systemd, durable)
                                      │  pi --print --agent orchestrator < task
                                      ▼
                              Pi orchestrator ──▶ its own subagents
                                      │  (session store JSONL, append-only)
                                      ▼
            turn terminal ──▶ wake event (HMAC webhook) ──▶ Hermes wake run
                                      │
                     acceptance check │ fail → pi_feedback (SAME session, ≤2 loops)
                                      ▼
                    result delivered to the originating channel (or log)
```

## What it is / is not

* **Is:** a small Python runner + CLI (`pi-bridge`), a Hermes user plugin
  (`pi-worker`, 5 tools), durable job state under `~/.local/state/pi-bridge/`,
  an optional self-waking webhook, and an optional read-only PI WEB observability
  view.
* **Is not:** a replacement for either agent. It never touches Hermes core, Pi,
  pi-open-agents or PI WEB sources; it never installs or updates them; it never
  picks Pi subagents (the Pi orchestrator does); Pi jobs do not depend on the
  Hermes gateway being alive (systemd user units + durable state).

## Prerequisites

| Component | Required | Notes |
|---|---|---|
| Hermes Agent | yes | user plugin mechanism + `hermes webhook`/gateway for wake; plugin needs no core patches |
| Pi Coding Agent | yes | CLI print mode with `--print --session-id --session-dir` (verified with Pi 0.84.x) |
| pi-open-agents (or equivalent) | yes | the bridge always runs `pi --agent orchestrator`, and `--agent` is provided by this extension (not pi core); it also gives the Pi orchestrator explorer/worker/reviewer. Any extension defining `--agent` + an `orchestrator` agent works the same |
| PI WEB | optional | read-only observability of delegated sessions; bridge works fully without it |
| systemd user session | strongly recommended | durable detached jobs; without it the runner falls back to a detached child that does not survive user-session restarts |
| Python 3.10+ | yes | for the bridge itself |

## Install

```bash
git clone https://github.com/motolight/pi-hermes-bridge.git
cd pi-hermes-bridge
./install.sh
```

`install.sh` is conservative by design: it checks prerequisites and compatibility
(Pi binary discovery + `pi --version` + print-mode capability probe, Hermes CLI
presence, systemd user session), creates a venv, installs the bridge CLI and
links it at `~/.local/bin/pi-bridge` (where the pi-worker plugin looks for it),
registers the Hermes plugin and offers to enable it. **It never installs/updates Hermes, Pi
or PI WEB** and never restarts the running gateway without asking. To enable the
completion wake channel (recommended) follow `docs/WAKE_SETUP.md` — it is a
documented operator step (HMAC secret + one route in Hermes config).

## Tools exposed to Hermes

| Tool | Purpose |
|---|---|
| `pi_delegate` | submit a task (returns immediately with `job_id`, captures delivery origin) |
| `pi_status` | status + compact final result (never the full transcript) |
| `pi_feedback` | continue the **same** Pi session with review notes |
| `pi_cancel` | safely stop a running job (bridge-created jobs only) |
| `pi_list` | recent jobs (id/status/cwd/task preview) — lost-context recovery |

## Features

* **Durable jobs** — per-job dir under `$PI_BRIDGE_HOME` (default
  `~/.local/state/pi-bridge`), atomic writes + flock, per-turn systemd units,
  restart-safe status reconciliation.
* **Self-waking Hermes** — after every terminal turn the runner POSTs an
  HMAC-signed webhook to the local Hermes gateway; the woken run does the
  acceptance check, reports or repairs (`pi_feedback`, ≤2 automatic loops), and
  delivers into the *originating* channel (per `docs/WAKE_SETUP.md`; no global
  fallback channel).
* **Recovery** — after any restart or lost model context, `pi_list` finds jobs;
  the wake client retries ~45 minutes through gateway restarts.
* **Pi Web observability (optional, read-only)** — `pi-bridge web-info <job>`
  and the `pi_web` field report whether the job's Pi session is visible in PI
  WEB, with a deep link; when PI WEB is down the bridge behaves exactly as
  before.
* **Safety** — no `shell=True`, task via stdin, argv only, generated job ids,
  bounded outputs, structured errors, secrets never stored in job state.

## Pi orchestrator example

The bridge always invokes `pi --agent orchestrator`; the `--agent` flag and the
`orchestrator` agent itself come from the pi-open-agents extension (or an
equivalent you configure) — configuring what that agent is (model, prompt,
allowed subagents) is Pi's own job. A minimal pi-open-agents setup:
`~/.pi/agent/agents/orchestrator.md` declaring
`allowedAgents: explorer, worker, reviewer` — see `docs/PI_ORCHESTRATOR_EXAMPLE.md`.
No model is prescribed.

## Routing policy

How Hermes decides *when* to delegate lives in `docs/ROUTING_POLICY.md`:
delegate substantial code/scripts/Docker/systemd/infra work; keep quick read-only
checks and non-technical requests local; one logical task = one job; never
prescribe subagents. Hermes ships this as a short always-loaded snippet plus an
optional skill — no core patches.

## Compatibility / self-test

```bash
.venv/bin/pi-bridge install            # discovery + capability check
python -m pytest tests/ -q             # 71 tests, fake pi / fake PI WEB / fake webhook receiver
```

## Uninstall / rollback

```bash
hermes plugins disable pi-worker
hermes plugins remove pi-worker        # or: rm -rf ~/.hermes/plugins/pi-worker
./uninstall.sh                         # asks first
```

`uninstall.sh` disables and removes the Hermes plugin `pi-worker`, removes the
CLI symlink `~/.local/bin/pi-bridge`, and — only on request — deletes the bridge
state under `${PI_BRIDGE_HOME:-~/.local/state/pi-bridge}`. It does **not** delete
the checkout (including its `.venv`); if you ask it to, it only prints the
`rm -rf` command for you to run yourself.

Hermes, Pi, pi-open-agents and PI WEB are left untouched.

## Security

See [SECURITY.md](SECURITY.md). Key points: webhook is loopback-only + HMAC V2
with timestamp binding and idempotency; task text never touches a shell; PI WEB
integration is read-only and optional; job state contains no secrets.

## License

MIT (see [LICENSE](LICENSE)). Third-party components (Hermes, Pi, pi-open-agents,
PI WEB) remain under their own licenses; this project only integrates them through
their public plugin/CLI interfaces.
