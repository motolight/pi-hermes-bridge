# Stall watchdog (v0.5.0) — architecture and operating notes

The stall watchdog is the bridge's own server-side supervisor: when nobody
is watching, it detects a genuinely stalled running Pi job, diagnoses it
read-only, attempts a bounded safe recovery, tells Hermes about it, and
escalates to a human only after the bounded attempts fail. It is completely
independent of any browser, WebUI or pi-lamp connection — pi-lamp is pure
observability on top.

## Components

| Piece | Role |
|---|---|
| `pi_bridge/watchdog.py` | detection, diagnosis, tier-1 recovery, event spool, `job["watchdog"]` state machine |
| `pi-bridge watchdog tick` | one evaluation pass; the systemd timer's `ExecStart` |
| `pi-bridge-watchdog.timer` / `.service` | bridge-owned user units: a oneshot tick every minute (`hermes-setup watchdog-install`) |
| `pi-bridge recover <job_id>` / plugin tool `pi_recover` | the same tier-1 recovery as a manual, job-id-only operator tool |
| webhook route `pi-bridge-stall` | separate event type (`pi_bridge_stall`) + prompt + policy, sibling of the completion route on the same platform/secret |
| `extensions/pi-lamp` writer 1.4 / JS | colors: blue working, amber pulse silent-checking, orange recovering, red needs_attention, blue again after recovery |

## Where the state lives

`job["watchdog"]` is an **additive block**; `job.status` is never touched by
the watchdog. States: `healthy → suspected_stall → diagnosing → recovering
→ recovered | needs_attention`. Terminal jobs belong to the normal v0.4.2
completion pipeline; a tick that races completion finds the job non-active
and does nothing. Every mutation happens under `state.job_lock` after a
fresh read; ticks are serialised by a non-blocking flock
(`<home>/watchdog/tick.lock`). Audit trail: `jobs/<id>/watchdog.jsonl`
(one JSON line per event: suspected / insufficient-evidence /
stall-confirmed / recover-tier1 / recovered / needs-attention / events),
plus `<home>/watchdog/{heartbeat.json,pending/,events/}`.

## Stall detection is never a single timer

"No JSONL for N minutes" alone only makes the job **suspected** (amber
silent-checking; then the watchdog snoozes and rechecks when evidence is
insufficient). Acting requires a multi-signal confirmation in one
`diagnose()` pass:

1. `job.status == running` **and** the runner alive (pid or systemd unit);
2. the current turn's **Pi PID alive with identity verified** via the
   recorded `/proc` start-time ticks (PID-reuse safe);
3. activity stale past the threshold — the newest of the session JSONL, the
   turn's stdout/stderr, `runner.log`, turn start;
4. Pi has **same-user descendant tool commands** (strict subtree), and they
   predate the observation window ("an old descendant command");
5. **CPU of that subtree is zero across repeated `/proc` samples**
   (two samples per pass; a process vanishing or reparenting mid-sample
   counts as *active*);
6. backend corroboration when configured (`backend_metrics_url`, e.g. the
   local vLLM `/metrics`): `vllm:num_requests_running + waiting == 0`
   supports the picture; a **busy** backend defers one confirmation
   (shared-backend safety) but never vetoes twice, and an unreachable
   backend is an unknown signal that never gates the decision.

A long build/test/download with real CPU work (4) or periodic output (3)
can therefore never satisfy the confirmation, at any silence length.

## Recovery tiers

**Tier 1 — automatic (the only automatic action).** Interrupt ONLY verified
descendant tool commands of that one job: every PID's ancestry is re-walked
to the identity-verified Pi PID and its start-time ticks are re-checked at
the moment of signalling; the Pi PID and the runner PID are excluded by
construction (the runner is an ancestor; Pi itself is not a strict
descendant of itself); same-user filter by `/proc` ownership; SIGTERM
first, bounded `term_grace_s`, then SIGKILL only of survivors. Pi stays
alive, so the hung tool call returns an error to Pi and Pi continues
reasoning in the SAME session — the JSONL resumes, and the next tick
**confirms** `recovered` (activity past the pre-signal baseline), never
assuming it.

**Tier 2 — diagnosed, never automatic.** Pi itself silent with nothing to
interrupt (e.g. a wedged model call) is reported (`pi-silent-no-descendants`)
and, after two identical strikes, escalated to `needs_attention`. There is
no automatic Pi kill. Design for a future tier 2 (session-preserving
restart of the model call) must keep this property: evidence-tiered, never
mixed with tier 1.

Bounded: at most `max_recovery_attempts` (default **2**) automatic tier-1
attempts, then `needs_attention`: a `pi_bridge_stall` event with
`severity=attention` to the Hermes stall route plus a direct user message
along the job's origin channel (`hermes send`, argv-only, messaging
platforms; webui origins learn it from the wake run and the red pi-lamp
row). A manual `pi-bridge recover` / `pi_recover(job_id)` does not consume
the automatic budget and still confirms via the same window.

## Hermes integration

Separate event type and policy — the read-only acceptance wake of v0.4.x
is untouched:

* payload `event_type=pi_bridge_stall`, `severity=stall|attention`, job,
  turn, task preview, the compact `watchdog` view, diagnosis summary and
  the recovery action/result;
* delivered to the bridge-owned route `pi-bridge-stall` (same HMAC V2
  scheme, same platform and secret as `pi-bridge-complete`; URL derived by
  swapping the route segment of the wake URL);
* `X-Request-ID = <job_id>-t<turn>-<severity>` is constant across retries,
  so the gateway's idempotency cache collapses duplicates exactly like the
  completion wake;
* **durable**: events are spooled under `<home>/watchdog/pending/` and
  retried by every later tick until accepted or expired
  (`event_expire_s`, ~45 min — the same window as the completion wake).
* The stall route's prompt gives the woken run a strictly bounded path:
  read-only checks, and at most ONE `pi_recover` of that job when state is
  `needs_attention`; never delegate, never feedback, never deliver.

## Configuration

`<bridge_home>/config.json` → `{"watchdog": {...}}`, overridden by
`PI_BRIDGE_WATCHDOG_<KEY>`:

| key | default | meaning |
|---|---|---|
| `enabled` | true | master switch (tick keeps running, evaluates nothing) |
| `stall_after_s` | 900 | silence before suspicion (the 10–15 min window; correctness does not depend on this default) |
| `startup_grace_s` | 90 | a freshly started turn is not a stall |
| `cpu_sample_interval_s` | 2.0 | between `/proc` CPU samples in one diagnosis |
| `term_grace_s` | 5.0 | SIGTERM → SIGKILL grace inside tier 1 |
| `confirm_grace_s` | 180 | window in which the resumed JSONL must appear after tier 1 |
| `max_recovery_attempts` | 2 | automatic tier-1 attempts before `needs_attention` |
| `backend_metrics_url` | "" | vLLM-style `/metrics`; "" disables the signal |
| `event_expire_s` | 2700 | stall-event retry budget |
| `descendant_min_age_s` | 5 | a tool command that just appeared is not interrupted |

## Race-safety contract

tick vs normal completion: `recover()` holds the job lock for planning,
re-checks the job is active and the turn unchanged before EVERY signal, and
re-validates each PID's start-time at signal time; `_tick` skips
non-active jobs and jobs whose runner is gone (reconcile owns those); the
watchdog never writes `job.status`, `cancel_requested` or any completion
field, and never competes with the delivery/wake pipeline.

## Production incident this feature is built on

See [INCIDENT_2026-10-21_PB-20261021T191120Z.md](INCIDENT_2026-10-21_PB-20261021T191120Z.md)
— the regression case pinned by `tests/test_watchdog.py`.
