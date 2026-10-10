#!/usr/bin/env python3
"""Fake `hermes` CLI for origin-delivery tests (see pi_bridge/deliver.py).

Records every invocation as one JSON line in $FAKE_HERMES_ARGV_OUT (jsonl),
then behaves like the real CLI per $FAKE_HERMES_MODE:

  ok         exit 0, print "sent" / the quiet-mode answer   (default)
  refused    exit 1 + `hermes-refusal-reason: SESSION_NOT_OWNED` on stderr
             (what a live-leased webui session answers)
  notfound   exit 1 + "Session not found: <id>" on stderr
  blocked    exit 1 + the dangerous-command gate's denial text (the failure
             mode that lost the 2026-10-07 result inside an agent run)
  sleep      sleep $FAKE_HERMES_SLEEP (default 30) then exit 0  (timeouts).
             On SIGTERM writes "TERM" to $FAKE_HERMES_TERM_MARK and exits, so
             a test can prove the bridge terminates before it kills.

Never touches the network, the real ~/.hermes or any real session.
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

MODE = os.environ.get("FAKE_HERMES_MODE", "ok")


def main() -> int:
    argv = sys.argv[1:]
    out = os.environ.get("FAKE_HERMES_ARGV_OUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps(argv, ensure_ascii=False) + "\n")

    if MODE == "sleep":
        def _on_term(signum, frame):
            mark = os.environ.get("FAKE_HERMES_TERM_MARK")
            if mark:
                with open(mark, "a", encoding="utf-8") as f:
                    f.write("TERM\n")
            sys.exit(143)
        signal.signal(signal.SIGTERM, _on_term)
        deadline = time.time() + float(os.environ.get("FAKE_HERMES_SLEEP", "30"))
        while time.time() < deadline:
            time.sleep(0.1)
        print("sent")
        return 0
    if MODE == "refused":
        print("hermes-refusal-reason: SESSION_NOT_OWNED", file=sys.stderr)
        print("Session already has a live owner (webui, pid 42).",
              file=sys.stderr)
        return 1
    if MODE == "notfound":
        print("Session not found: nope", file=sys.stderr)
        return 1
    if MODE == "blocked":
        print("BLOCKED: Command timed out without user response. The user "
              "has NOT consented to this action. Silence is not consent.",
              file=sys.stderr)
        return 1
    print("sent" if argv[:1] == ["send"] else "ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
