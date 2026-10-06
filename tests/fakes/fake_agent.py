#!/usr/bin/env python3
"""Tiny print-mode turn emulator for the fake `pi` CLI.

`pi --print --agent <name> <prompt>` succeeds only when the agent file
<PI_CODING_AGENT_DIR|HOME/.pi/agent>/agents/<name>.md exists — exactly the
contract the installer's orchestrator probe relies on.
"""
import os
import sys
from pathlib import Path


def main() -> int:
    argv = sys.argv[1:]
    agent = None
    skip_next = False
    prompt = None
    for a in argv:
        if skip_next:
            agent = a
            skip_next = False
            continue
        if a == "--agent":
            skip_next = True
            continue
        if a in ("--print", "--session-id", "--session-dir"):
            continue
        if a.startswith("--"):
            continue
        prompt = prompt or a
    if agent is None and "--agent" in argv:
        print("error: --agent without a value", file=sys.stderr)
        return 2
    home = Path(os.environ.get("PI_CODING_AGENT_DIR")
                or Path.home() / ".pi" / "agent")
    if agent is None:
        print("error: no agent selected", file=sys.stderr)
        return 2
    if not (home / "agents" / f"{agent}.md").is_file():
        print(f"error: unknown agent '{agent}'", file=sys.stderr)
        return 3
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
