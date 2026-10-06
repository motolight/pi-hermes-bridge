# Example Pi orchestrator + subagents configuration

The bridge always runs `pi --print --agent orchestrator`. What that agent *is*
belongs to Pi's own configuration — nothing here is required by the bridge, and
no model is prescribed. This example assumes the `pi-open-agents` extension
(explorer/worker/reviewer roles); any other subagent scheme works the same,
because the bridge never inspects or steers subagents.

```
~/.pi/agent/
├── models.json              # your models/providers (Pi's own format)
└── agents/
    ├── orchestrator.md      # main delegated agent
    ├── explorer.md          # read-only research subagent
    ├── worker.md            # implements & verifies
    └── reviewer.md          # independent acceptance check
```

`agents/orchestrator.md` frontmatter (body = your orchestrator prompt):

```markdown
---
name: orchestrator
tools: read, bash, edit, write, grep, find, ls
model: <any model you run — local vLLM, hosted, etc.>
thinking: medium
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
  after every terminal turn, so a long job may wake Hermes several times
  (queued→running→completed transitions and intermediate turns).
* **Session continuity** (`--session-id` print mode) is what makes `pi_feedback`
  continue the same orchestrator conversation — including whatever subagents it
  had already used.
* Subagent transcripts live in the Pi session store next to the main session
  (e.g. a `subagents/` subdirectory); PI WEB shows the main transcript, with
  subagent activity embedded as tool-call records.
