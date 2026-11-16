# Hermes -> Pi: Attest regression stand

Status (2026-11-16): WIP. The current branch contains only deterministic
bridge smoke tests, NOT a genuine Attest model-replay run. A green smoke suite
must not be represented as end-to-end acceptance of a real Hermes + Pi agent.

## Implemented

- bash test-hermes-pi.sh: detached disposable git worktree from HEAD
- Each test uses the CURRENT pi_bridge runner/CLI and isolated bridge HOME,
  but a scripted fake Pi. No live LLM or production agent.
- Five PASS/FAIL lines, non-zero exit on failure. PR CI runs these tests.
- Coverage: file edit + WebUI spool, long job + wake, closed/reopened
  gateway port + retry, failed Pi job with no loop, edit of disposable
  git repo + successful test execution.
- The WebUI receipt test emulates the consumer. It is NOT evidence that
  Hermes' native async-completion ledger received the event.

## Required before Attest acceptance (not yet implemented)

1. Create unprivileged test installs of Hermes, Pi and pi-open-agents in a
   disposable filesystem sandbox. Use isolated HOME, HERMES_HOME,
   PI_CODING_AGENT_DIR, PI_BRIDGE_HOME, job/session IDs, and git worktree.
   Deny production mounts, credentials, SSH agent, Docker socket, messaging
   targets, production endpoints and shell access outside the worktree.
2. Capture a clean real successful run of EACH scenario through Hermes and
   Pi with Hermes observer/OTel, and use Attest capture proxy for EVERY
   underlying LLM client. An OTel-only trace cannot replay model exchanges.
   Verify clients honor the proxy across both agent processes.
3. Review all traces/cassettes for secrets and sensitive user content.
   Pin Attest + engine versions and model/agent/bridge version hashes.
   Never auto-refresh golden fixtures after upgrades.
4. Strict replay must execute today's REAL Hermes/Pi/bridge code with
   ATTEST_REPLAY=strict, ATTEST_EGRESS=block and no live inference credentials.
   Fail closed on missing cassette, unrecorded exchange, prompt/schema miss,
   unexpected tool calls, too many steps, side-effect mismatch, error or
   timeout. Require zero live model calls; do not stub real file/git ops.
5. Verify delivery beyond job status: native Hermes completion injection,
   ledger acknowledgment, correct session/recipient and exactly once across
   a restart of the sandbox gateway. Never assert success from a mere
   pending webui_delivery.json file.
6. Check five Attest-backed acceptance contracts: delegation and edit;
   long task delivery; gateway restart; bounded Pi error; real coding edits
   temporary git repo and leaves passing tests. Validate tool trajectories,
   max steps, retries, process cleanup and final side effects.

Target user UX: ./test-hermes-pi.sh runs both smoke and strict Attest replay,
emits just five PASS/FAIL lines and exits nonzero on any failure or missing
prerequisite. The CURRENT shell script is only the smoke layer, and cannot
yet be accepted as the requested end-to-end solution.

## Live canary (separate, real inference)

Run one real Hermes -> Pi coding task ONLY after model/vLLM/tool-call stack
changes. Within a restricted test sandbox, present a broken test in a small
git repo; require actual Pi to fix code, tests to pass, and Hermes to receive
the completion. Report model revision, tokens, wall time and PASS/FAIL.
Do not use a replay cassette or fake Pi for this test.

## Non-negotiable isolation

Do not run install.sh, uninstall.sh, or hermes gateway restart against
production during tests. Do not alter real ~/.hermes, ~/.pi or PI_BRIDGE_HOME.
Never interrupt an existing Pi job. All fixtures and status paths must be
inside test-only directories. Strict replay is not a security sandbox by
itself; its tool calls still need OS-level containment.
