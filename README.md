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
  (`pi-worker`, 6 tools), durable job state under `~/.local/state/pi-bridge/`,
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
   installed + enabled (6 tools), with one controlled
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
| `pi_recover` | tier-1 stall recovery for ONE job: interrupts only that job's verified descendant tool commands (never Pi, never arbitrary PIDs) |

## Features

* **Durable jobs** — per-job dir under `$PI_BRIDGE_HOME` (default
  `~/.local/state/pi-bridge`), atomic writes + flock, per-turn systemd units,
  restart-safe status reconciliation.
* **Outcome delivery by origin (V1.5)** — after every terminal turn the
  runner hands the result back along the job's recorded origin: `hermes send
  -t <platform>:<chat_id>[:<thread_id>]` for messaging platforms; for `webui`
  the runner never runs a Hermes CLI at all — it records a durable delivery
  request next to the job, and the bridge's Hermes plugin injects the
  completion into Hermes' own native background-completion pipeline inside
  the process that hosts the session (a real acceptance turn at the idle
  boundary, never a user-looking message, no session-lease contention; see
  [`docs/WEBUI_COMPLETION_EVENT_SPEC.md`](docs/WEBUI_COMPLETION_EVENT_SPEC.md));
  and *nothing along the origin* for an empty/`webhook`/`local`/unknown origin
  (no default channel). If the operator wants finished cron/wake jobs to be
  announced anyway, `wake.json` `fallback_channel` (e.g.
  `"telegram:261266401"`) sends exactly **one** copy of that same summary to
  one operator-owned chat, recorded as `channel: "<platform>-fallback"` — the
  target is read from the operator's config or environment and never from a
  job, so no submitter can redirect a result. argv only, never a shell, hard
  timeout, no retries — so a result
  that merely looks like a dangerous command cannot be stuck behind an
  approval gate nobody can answer in a webhook session.
* **Self-waking Hermes** — then the runner POSTs an HMAC-signed webhook to the
  local Hermes gateway; the woken run does a **read-only** acceptance check and
  either repairs (`pi_feedback`, ≤2 automatic loops) or ends. It never delivers
  anything itself. `install.sh` configures this channel for you (log-only
  route).
* **Recovery** — after any restart or lost model context, `pi_list` finds jobs;
  the wake client retries ~45 minutes through gateway restarts.
* **Poller observability (V1.5, v0.4.2)** — the plugin's completion poller
  decides hosting by *process role* (argv/env/main script, not lazy imports:
  the WebUI backend stays a valid host even after it lazy-imports
  `gateway.run`), burns a durable per-request `attempts` counter on every
  real injection attempt, and keeps a per-pid health record under
  `$PI_BRIDGE_HOME/health/` (role, gate decision, last tick/scan, pending
  count, last lifecycle jobs, counters — never task/result text), read back
  with the read-only `pi-bridge health [--json]`.
* **Pi Web observability (optional, read-only)** — `pi-bridge web-info <job>`
  and the `pi_web` field report whether the job's Pi session is visible in PI
  WEB, with a deep link; when PI WEB is down the bridge behaves exactly as
  before.
* **Autonomous stall watchdog (v0.5.0)** — a bridge-owned systemd user timer
  runs `pi-bridge watchdog tick` every minute: a running job that goes silent
  is SUSPECTED, then diagnosed with multiple independent read-only signals
  (identity-verified Pi PID, stale session JSONL/stdout, zero-CPU descendant
  tool commands across repeated /proc samples, optional vLLM `/metrics`
  corroboration), and tier-1-recovered automatically — only the job's verified
  descendant tool commands are interrupted, never Pi itself, so the session
  resumes in place. Bounded (≤2 attempts), then `needs_attention` with a user
  message plus a `pi_bridge_stall` event to Hermes on a separate route/policy.
  Server-side by design: independent of any browser, WebUI or pi-lamp
  connection. See [`docs/STALL_WATCHDOG.md`](docs/STALL_WATCHDOG.md).
* **Durable FIFO job queue** — Pi and Hermes share one inference backend, so
  every concurrently running job (and every subagent inside it) is a separate
  competing request. The queue caps how many jobs execute at the same time:
  by default **one heavy job plus one light/urgent job**, the rest wait. It is
  strictly FIFO inside a class and durable in the simplest possible way — a
  waiting job is just a `queued` job with no runner anchor in `job.json`, so
  it survives a gateway/runner restart and a reboot with nothing extra to
  replay, and jobs already running when you turn the queue on are never
  touched. Configure it in `<bridge_home>/config.json` or with the
  environment; see [Job queue](#job-queue-gpu-capacity-control).
* **Safety** — no `shell=True`, task via stdin, argv only, generated job ids,
  bounded outputs, structured errors, secrets never stored in job state.
* **Live WebUI status badges (optional, `extensions/pi-lamp/`)** — a
  sidecar-free Hermes WebUI extension: a read-only 10 s writer snapshots job
  state to `status.json`, and per-chat badges count running / finished /
  failed `pi_delegate` jobs with a detail card, stall detection and optional
  local PI WEB deep links. Since writer 1.3 a finished webui-origin job is
  amber ("Pi done · waiting for Hermes") until Hermes' native completion
  delivery is acknowledged, green only then, and red when the delivery was
  abandoned — the lamp no longer lies green about an unreceived result.
  No new port, no sidecar, no LLM in the loop; see
  [`extensions/pi-lamp/README.md`](extensions/pi-lamp/README.md).

## Job queue (GPU capacity control)

Pi and Hermes call the same model, so the number of simultaneously running Pi
jobs is the capacity knob of the whole setup. Admission is a single FIFO pass
over the jobs directory: a job that may run gets its runner launched, the
rest wait in `queued`.

**Slots and classes.** Every job has a class: `heavy` (the default — exactly
the pre-queue behaviour) or `light` for quick/interactive work:

```
pi-bridge submit --light --cwd DIR --task "..."
```

Each class has its own slot budget (default `1` heavy + `1` light), and a job
only consumes a slot of its own class. That is why the queue is FIFO *per
class*: an urgent light job never waits behind a long heavy one, and a light
job never occupies the heavy slot (a single global FIFO would make the extra
interactive slot useless). A feedback turn re-enters the queue like any other
execution, with the job's own class.

**Durability.** A waiting job is a `queued` job whose `last_run` is still
`null` — the queue has no separate ledger, so restarting the gateway or the
runner (or rebooting the host) cannot lose it. Jobs that were already running
before the queue existed keep their slot and finish normally.

**Pumps.** Admission runs on `submit`/`feedback` (so an idle bridge still
starts the job immediately), when a runner finalizes a turn, on `cancel`, from
the bridge-owned watchdog systemd timer (the durable pump that a restart
cannot stop) and manually:

```
pi-bridge queue status        # slot budget, what runs, who waits and in what order
pi-bridge queue pump          # run one FIFO admission pass now
```

**Configuration** — `<bridge_home>/config.json` (default
`~/.local/state/pi-bridge/config.json`), all keys optional:

```json
{
  "queue": {
    "enabled": true,
    "heavy_slots": 1,
    "light_slots": 1
  }
}
```

| Environment (highest priority) | Meaning |
|---|---|
| `PI_BRIDGE_QUEUE=0` | switch the queue off: `submit` launches the runner immediately, exactly like bridge ≤ 0.5.0 |
| `PI_BRIDGE_QUEUE_HEAVY_SLOTS=N` | heavy slot budget (`0` is valid: heavy jobs do not start) |
| `PI_BRIDGE_QUEUE_LIGHT_SLOTS=N` | light slot budget |

Capacity is read on every pass, so changing the budget takes effect from the
next pump — no restart. A single pass never launches more than 8 jobs, however
large the configured budget is. `pi_status`/`pi_list` carry the queue view
(`class`, `state: waiting|running|finished`, `position` in line); it is purely
informational and never changes job semantics.

The measurements behind these defaults — a before/after run of the same job
package, the verified Pi compaction threshold and the rollback command for
each knob — are in [`docs/PERF_CAPACITY.md`](docs/PERF_CAPACITY.md).

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
pi-bridge health [--json]              # read-only poller observability (V1.5)
python -m pytest tests/ -q             # full core suite: fake pi / fake PI WEB / fake webhook receiver / fake hermes + installer e2e
python -m pytest extensions/pi-lamp/tests -q && \
  node extensions/pi-lamp/tests/js/smoke.js extensions/pi-lamp/assets/pi-lamp.js <status.json>   # pi-lamp writer + browser reader
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
