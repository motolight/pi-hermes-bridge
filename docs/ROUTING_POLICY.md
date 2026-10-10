# Routing policy: when Hermes delegates to Pi

This is the short, always-loaded policy snippet (drop into Hermes `SOUL.md` or an
equivalent always-on context file — Hermes' own mechanism; the bridge ships no
core patch). Keep it under ~600 characters; it costs tokens on every turn.

```text
# Pi delegation (pi-worker plugin)
Substantial technical work — code changes, scripts to run, Docker/systemd/DevOps,
infra diagnosis/repair, repo operations — MUST be delegated to the Pi orchestrator
via pi_delegate (never inline with terminal/file tools). Trivial read-only checks
(one command, one file, grep/curl, status) and non-technical/research/conversational
requests stay with Hermes. Before delegating when unsure a job already exists —
pi_list. After pi_delegate: tell the user it was handed to Pi, always include the
job_id, end the turn. On Pi completion wake: pi_status → independent acceptance
check (read-only tools only) → on failure pi_feedback in the same session (max
2 repair loops) → otherwise just end the turn; the bridge delivers the outcome
to the job's origin channel itself, so a wake run never sends or resumes.
```

## Rationale / expanded rules

1. **Delegate** non-trivial development: code changes, repository operations,
   shell/Python scripts, Docker/systemd/DevOps, infrastructure diagnosis and
   repair.
2. **Keep local**: simple read-only commands, short status checks, reading one
   file, simple grep/curl, and similar small things. Delegation overhead is real.
3. **Don't delegate** conversational, research, search, and non-technical tasks
   just because Pi is available.
4. **One logical task = one job.** Don't fragment one goal into many independent
   Pi jobs.
5. Hermes **never picks** explorer/worker/reviewer or dictates subagent structure
   — always hand the goal to the Pi orchestrator; it manages its own team.
6. Pass to Pi: the user's goal, relevant environment context, constraints, what
   must not break, and acceptance criteria. Don't over-design the solution for Pi.
7. **Approval gates** for dangerous/destructive actions stay as Hermes/Pi already
   enforce them.
8. After `pi_delegate`: briefly tell the user it was handed to Pi **with the
   job_id**, then end the turn — don't stall the foreground conversation.
9. On completion wake: (1) `pi_status` (status, error, result, origin,
   `delivery`), (2) independent acceptance check with **read-only** tools,
   (3) fail → `pi_feedback` into the same session, (4) max 2 automatic repair
   loops, (5) otherwise end the turn with a one-line outcome. **Never deliver
   from a wake run** — the runner hands the outcome to the job's origin
   channel itself before waking (V1.4), and an approval-gated send from a
   webhook session has nobody to approve it.
10. Before a *repeat* delegate when job existence is uncertain → `pi_list` first
    (duplicate prevention).
11. PI WEB stays optional observability; it never influences routing/execution.
