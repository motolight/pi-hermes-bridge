#!/usr/bin/env python3
"""Fake `pi` executable for bridge tests.

Emulates the subset of pi 0.84.4 print-mode behavior the bridge relies on:
- reads task from non-TTY stdin;
- --print --agent X --session-id ID [--session-dir DIR];
  without --session-dir it uses pi's standard store, $HOME/.pi/agent/sessions
  (exactly what the bridge does since V1.1, so PI WEB can see the session);
- session file at <store>/<normalized-cwd>/<ts>_<ID>.jsonl, appended per turn;
- prints ONLY the final assistant text to stdout; exit 0 on success;
- non-zero exit + stderr text on failure.

Task control markers:
- BRIDGE_FAIL          -> stderr + exit 3
- BRIDGE_SLEEP <sec>   -> sleep (long task)
- BRIDGE_BIG           -> print ~1MB of text
- BRIDGE_ECHO_TASK     -> print the exact task bytes between sentinels
- otherwise            -> print "FAKEOK ..." or "RESUMED sid=... turns=N ..."

Env: FAKE_PI_ARGV_OUT=<path> -> dump the received argv as JSON to <path>
before doing anything else (used to assert the bridge's pi invocation).
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def normalize_cwd(cwd: str) -> str:
    return "--" + re.sub(r"[^a-zA-Z0-9]", "-", cwd) + "--"


def main() -> int:
    argv = sys.argv[1:]
    argv_out = os.environ.get("FAKE_PI_ARGV_OUT")
    if argv_out:
        Path(argv_out).write_text(json.dumps(argv))
    if "--version" in argv:
        print("fake-pi 0.0.0")
        return 0
    session_id = None
    session_dir = None
    agent = None
    expect = None
    for a in argv:
        if expect == "sid":
            session_id, expect = a, None
            continue
        if expect == "dir":
            session_dir, expect = a, None
            continue
        if expect == "agent":
            agent, expect = a, None
            continue
        if a == "--session-id":
            expect = "sid"
        elif a == "--session-dir":
            expect = "dir"
        elif a == "--agent":
            expect = "agent"
    if "--print" not in argv and "-p" not in argv:
        print("fake-pi: expected --print", file=sys.stderr)
        return 64
    if not session_id:
        print("fake-pi: missing --session-id", file=sys.stderr)
        return 65
    if session_dir is None:
        # pi's default session store (PI WEB reads this same directory)
        session_dir = str(Path.home() / ".pi" / "agent" / "sessions")
    if agent != "orchestrator":
        print(f"fake-pi: bad agent {agent!r}", file=sys.stderr)
        return 66

    task = sys.stdin.read()

    if "BRIDGE_FAIL" in task:
        print("fake-pi boom: simulated model error", file=sys.stderr)
        return 3

    m = re.search(r"BRIDGE_SLEEP (\d+(?:\.\d+)?)", task)
    if m:
        deadline = time.time() + float(m.group(1))
        while time.time() < deadline:
            time.sleep(0.1)

    cwd = os.getcwd()
    sdir = Path(session_dir) / normalize_cwd(cwd)
    sdir.mkdir(parents=True, exist_ok=True)
    existing = sorted(sdir.glob(f"*_{session_id}.jsonl"))
    if existing:
        path = existing[0]
        turns = len(existing[0].read_text().strip().splitlines()) if existing else 0
    else:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
        path = sdir / f"{ts}_{session_id}.jsonl"
        path.write_text("")
        turns = 0

    entry = {
        "type": "message",
        "at": datetime.now(timezone.utc).isoformat(),
        "role": "user",
        "text": task,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    if "BRIDGE_BIG" in task:
        print("X" * (1024 * 1024))
        return 0

    if "BRIDGE_ECHO_TASK" in task:
        body = task.replace("BRIDGE_ECHO_TASK", "")
        sys.stdout.write("TASK-BEGIN[" + body + "]TASK-END\n")
        return 0

    if turns >= 1:
        print(f"RESUMED sid={session_id} turns={turns} last_task_len={len(task)}")
    else:
        print(f"FAKEOK sid={session_id} task_len={len(task)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
