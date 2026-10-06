# Example Pi orchestrator + subagents configuration

The bridge always runs `pi --print --agent orchestrator`. The `--agent` flag and
the `orchestrator` agent are not pi-core built-ins — they come from your pi
configuration, in practice from the `pi-open-agents` extension
(explorer/worker/reviewer roles), which is the setup this example assumes. Any
other extension that provides `--agent` and an `orchestrator` agent works the
same, because the bridge never inspects or steers subagents.

```
~/.pi/agent/
├── models.json              # your models/providers (Pi's own format)
└── agents/
    ├── orchestrator.md      # main delegated agent
    ├── explorer.md          # read-only research subagent
    ├── worker.md            # implements & verifies
    └── reviewer.md          # independent acceptance check
```

`agents/orchestrator.md` frontmatter (body = your orchestrator prompt; this is
also the shape `install.sh` creates — it omits the `model:` line so pi uses
your current/default model, and adds an optional `permission:` block that you
can tighten afterwards):

```markdown
---
name: orchestrator
tools: read, bash, edit, write, grep, find, ls
model: <any model you run — local vLLM, hosted, etc.>   # omit to use pi's default
thinking: medium
mode: primary
allowedAgents: explorer, worker, reviewer
---
You are the orchestrator. Decompose the goal, delegate to subagents as you see
fit, verify their output yourself, and end your turn only when the acceptance
criteria are met. If you change code, run the relevant tests in the same turn.
```

`worker.md` frontmatter essentials:

```markdown
---
name: worker
tools: read, bash, edit, write, grep, find, ls
model: <worker model>
pipeline:
  - reviewer
---
```

Key properties that matter for the bridge contract:

* The **orchestrator's turn end** is the job's wake trigger — the bridge fires
  when a turn ends `completed` or `failed`. One `pi_delegate` wakes Hermes
  once; every `pi_feedback` turn that then completes wakes Hermes again.
* **Session continuity** (`--session-id` print mode) is what makes `pi_feedback`
  continue the same orchestrator conversation — including whatever subagents it
  had already used.
* Subagent transcripts live in the Pi session store next to the main session
  (e.g. a `subagents/` subdirectory); PI WEB shows the main transcript, with
  subagent activity embedded as tool-call records.
