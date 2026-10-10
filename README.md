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
  checks the actual result with read-only tools, and can send concrete feedback
  back into the same Pi session for another repair turn. The *user-visible*
  delivery is not part of that loop: the runner delivers the outcome along the
  job's origin channel itself before waking anyone.
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
            turn terminal ──▶ origin delivery (hermes send / --resume, argv)
                              │  then wake event (HMAC webhook) ──▶ Hermes wake run
                              │            acceptance check │ fail → pi_feedback
                              ▼                             ▼        (SAME session, ≤2 loops)
        outcome reaches the originating channel     log only; the wake run
        (or nowhere, if the origin is unknown)      never sends messages
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
| Hermes Agent | yes | install it yourself beforehand — the installer refuses to (your credentials, your config); needs user plugin mechanism + `hermes webhook`/gateway; plugin needs no core patches |
| Pi Coding Agent | yes | likewise installed by you; CLI print mode with `--print --session-id --session-dir` (verified with Pi 0.84.x) |
| pi-open-agents (or equivalent) | yes | the bridge always runs `pi --agent orchestrator`, and `--agent` is provided by this extension (not pi core); it also gives the Pi orchestrator explorer/worker/reviewer. Any extension defining `--agent` + an `orchestrator` agent works the same. If missing, the installer *offers* to run `pi install npm:pi-open-agents` (consent required, no sudo) |
| PI WEB | optional | read-only observability of delegated sessions; bridge works fully without it |
| systemd user session | strongly recommended | durable detached jobs; without it the runner falls back to a detached child that does not survive user-session restarts |
| Python 3.10+ | yes | for the bridge itself |

## Install

```bash
git clone https://github.com/motolight/pi-hermes-bridge.git
cd pi-hermes-bridge
./install.sh
```

**The installer configures the Hermes plugin, routing policy and completion
wake channel for you. No manual SOUL.md or webhook editing is required on the
normal path.** Concretely, `install.sh`:

1. **Checks prerequisites** — Python ≥ 3.10, the Hermes CLI, the Pi CLI, Pi
   agent/orchestrator support (`--agent` + a real `orchestrator` agent), and a
   systemd user session for durable jobs. **Hermes and Pi must be installed
   beforehand and are deliberately NOT installed or updated by this
   installer** (security: they carry your model credentials and
   configuration); if one is missing the installer stops with the official
   project URL and changes nothing.
2. **Resolves the Pi orchestrator** — if a compatible `pi-open-agents`
   extension + `orchestrator` agent already exist, nothing is changed.
   Otherwise the installer *offers* to run `pi install npm:pi-open-agents`
   (pi's own package mechanism, no sudo, existing pi configs preserved) and
   can create a minimal `orchestrator` agent in pi-open-agents' standard
   format — prescribing **no model** (Pi's current/default model is used) and
   never picking subagents (that is the Pi orchestrator's own decision).
3. **Installs the bridge** — a venv in the checkout, the `pi-bridge` CLI
   linked at `~/.local/bin/pi-bridge`, the `pi-worker` Hermes plugin
   installed + enabled (5 tools), with one controlled
   `hermes gateway restart` at the end so everything is live immediately.
4. **Installs the routing policy** — a short always-on managed block in
   `~/.hermes/SOUL.md` between `<!-- pi-hermes-bridge:begin/end -->` markers
   plus the `pi-routing-policy` skill. Existing SOUL.md content is preserved;
   re-running updates the block instead of duplicating it.
5. **Configures the completion wake** — a fresh random HMAC secret (wake.json
   mode 0600), the `pi-bridge-complete` static route in
   `~/.hermes/config.yaml` (loopback bind, `deliver: log`, read-only V1.4
   acceptance prompt), and `$PI_BRIDGE_HOME/wake.json`. Foreign webhook routes,
   settings and a pre-existing manual route are detected and left untouched
   — no duplicates on upgrade from v0.1/manual setup.

Every change is owned and idempotent: run `./install.sh` as often as you
like; `./uninstall.sh` removes exactly the pieces above and nothing else.
Useful flags: `--yes` (non-interactive), `--no-restart` (skip the gateway
restart), `--allow-non-loopback-webhook` (only if you deliberately expose the
Hermes webhook platform beyond loopback — not recommended), env overrides in
`./install.sh --help`.


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
* **Outcome delivery by origin (V1.4)** — after every terminal turn the runner
  itself hands the result back along the job's recorded origin: `hermes send -t
  <platform>:<chat_id>[:<thread>]` for messaging platforms, `hermes --resume
  <ui_session_id> chat -q … -Q --source tool` for `webui`, and *nothing* for an
  empty/unknown origin (no default channel, no Telegram fallback). argv only,
  never a shell, hard timeout, no retries — so a result that merely looks like
  a dangerous command cannot be stuck behind an approval gate nobody can answer
  in a webhook session.
* **Self-waking Hermes** — then the runner POSTs an HMAC-signed webhook to the
  local Hermes gateway; the woken run does a **read-only** acceptance check and
  either repairs (`pi_feedback`, ≤2 automatic loops) or ends. It never delivers
  anything itself. `install.sh` configures this channel for you (log-only
  route).
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
prescribe subagents. `install.sh` installs this automatically as a short
always-on managed block in `SOUL.md` plus the portable
`skills/pi-routing-policy` skill — no core patches.

## Compatibility / self-test

```bash
.venv/bin/pi-bridge install            # discovery + capability check
python -m pytest tests/ -q             # 105 tests: fake pi / fake PI WEB / fake webhook receiver / fake hermes + installer e2e
```

## Uninstall / rollback

```bash
./uninstall.sh                         # removes only what the installer owns
```

`uninstall.sh` removes exactly the installer's own changes: the managed SOUL.md
routing block (your other SOUL text survives), the `pi-routing-policy` skill
(if we installed it), the bridge-owned `pi-bridge-complete` webhook route
(foreign routes/settings survive; a pre-existing manual route is left alone),
`wake.json`, the `pi-worker` plugin and the `~/.local/bin/pi-bridge` symlink.
Job state under `${PI_BRIDGE_HOME:-~/.local/state/pi-bridge}` is deleted only
after you literally type `DELETE` at the prompt (never auto-confirmed, even in
non-interactive mode). It does **not** delete the checkout (including its
`.venv`); if you ask it to, it only prints the `rm -rf` command for you to run
yourself.

Hermes, Pi, pi-open-agents and PI WEB are left untouched — pi-open-agents is
never auto-removed even if the installer once installed it.

## Security

See [SECURITY.md](SECURITY.md). Key points: webhook is loopback-only + HMAC V2
with timestamp binding and idempotency; task text never touches a shell; PI WEB
integration is read-only and optional; job state contains no secrets.

## License

MIT (see [LICENSE](LICENSE)). Third-party components (Hermes, Pi, pi-open-agents,
PI WEB) remain under their own licenses; this project only integrates them through
their public plugin/CLI interfaces.
