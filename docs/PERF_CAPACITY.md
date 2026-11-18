# GPU capacity: why jobs queue, and which knobs control it

Measured 2026-11-18 on this box (single vLLM, `qwen-local`,
`max_model_len = 262144`, Pi `contextWindow = 262144`, `maxTokens = 32768`).

## The bottleneck

Pi and Hermes call the same model on the same GPU. Nothing about the bridge
made the model faster; what changed is how many jobs share it at once. With
8-9 simultaneous jobs on one vLLM:

| week | answers/week | s/answer | generation tok/s | share > 30 s |
|---|---|---|---|---|
| W42-W45 (Oct) | 7.5-9.9k | 12-16 | 47-58 | 5-10 % |
| W46 (9-15 Nov) | 4.9k | 40 | 17 | 19 % |
| W47 (16-18 Nov) | 0.9k | 50 | 16 | 31 % |

The only capacity knob the bridge owns is therefore **how many Pi jobs run
concurrently** -- see `README.md` ("Queue") and `pi-bridge queue status`.

## Knob 1: job admission (this repository)

`config.json` → `queue.{enabled,heavy_slots,light_slots}`, env overrides
`PI_BRIDGE_QUEUE`, `PI_BRIDGE_QUEUE_HEAVY_SLOTS`,
`PI_BRIDGE_QUEUE_LIGHT_SLOTS`. Defaults: enabled, 1 heavy + 1 light.

Measured with the identical 4-job package (1 heavy + 3 light), free GPU, same
model, same tasks:

| metric | queue off | 1 heavy + 1 light |
|---|---|---|
| wall-clock of the package | 441 s | 399 s |
| max concurrent jobs | 4 | 2 |
| max concurrent model generations | 4 | 2 |
| median model answer | 14.6 s | 8.2 s |
| p90 model answer | 26.1 s | 21.0 s |
| share of answers > 30 s | 9.3 % | 1.6 % |
| generation throughput | 25.8 tok/s | 32.0 tok/s |
| preemptions | 0 | 0 |

Wall-clock did not regress: the second half of the package waits, but the
running pair is ~1.8x more responsive per step, which pays for the wait.

Rollback of the knob without a deploy: `"queue": {"enabled": false}` (or
`PI_BRIDGE_QUEUE=0`) -- read on every admission pass, so it takes effect for
the next submit immediately.

## Knob 2: subagent concurrency (Pi side, prompt-level only)

pi-open-agents 0.1.20 and 0.1.31 have **no** stock concurrency limit for
`subagent` -- only `maxDepth`/`allowedAgents` (upstream roadmap item #11 is
still open). Pi core 0.84.4 does have `toolExecution: "sequential"`, but it is
an SDK/bundle option, not a CLI or settings.json key, and in a `tool_call`
hook a blocking preflight would deadlock the batch.

What works today is a written rule in the orchestrator agent file
(`~/.pi/agent/agents/orchestrator.md`, section "GPU-sharing discipline"): at
most 2 `subagent` calls per assistant message. That is the file Pi jobs and
interactive orchestrator sessions read, so it applies without patching
anything. It is guidance, not a hard cap -- a queue slot is the hard cap.

## Knob 3: compaction threshold (Pi side)

Default `compaction.reserveTokens = 16384` means auto-compaction only fires at
`262144 - 16384 = 245760` tokens, i.e. a session drags a ~230 K-token prompt
for hours. Applied (in `~/.pi/agent/settings.json`):

```json
{"compaction": {"enabled": true, "reserveTokens": 196608, "keepRecentTokens": 20000}}
```

Threshold becomes `262144 - 196608 = 65536` tokens. Verified before applying
(same vLLM, project-scoped settings so production was untouched): a session
that grew to 72 051 tokens compacted automatically, context fell to 16 498
tokens, and afterwards the model still reproduced the decisions stated at the
very start of the session (storage choice, acceptance criteria, API version)
and the marker names from the first and last of six files read before the
compaction boundary.

Rollback: restore `~/.pi/agent/settings.json.bak-s1-20261118` (or delete the
`compaction` block to go back to Pi defaults). New value applies to sessions
started after the change.

## What this does NOT change

vLLM itself (no restart, no config change), the model, GPU pinning, the
Hermes plugin (`plugin/__init__.py` is a thin pass-through: `pi_delegate` =
`submit`, and `queued` was already a documented status), and production
Hermes. No `git push`.
