"""The browser reader must agree with the writer about what a badge says.

Runs writer.py over a synthetic bridge home, then hands the *real* status.json
it produced to the headless JS harness (tests/js/smoke.js).  The harness
re-derives every badge's number and colour straight from the snapshot and
asserts that the shipped assets/pi-lamp.js renders exactly that — so a writer
that changes a `group`, a threshold or a timestamp format without the reader
following fails here instead of in the browser.  Skipped when node is absent.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
sys.path.insert(0, str(HERE))

from test_writer import (JOB_B, JOB_C, JOB_D, JOB_E, JOB_OK, _turns,  # noqa: E402
                         make_job, run_writer)


def test_reader_agrees_with_writer_on_a_real_snapshot(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    root = tmp_path / "bridge"
    # chat sess-a: one running + a 2h failure + a 1h done + a 5h done(quiet)
    make_job(root, JOB_OK)
    make_job(root, JOB_B, status="failed", updated=str(-7200), error="boom",
             turns=_turns(finished_off=-7200, exit_code=1))
    make_job(root, JOB_C, status="completed", updated=str(-3600),
             turns=_turns(finished_off=-3600))
    make_job(root, JOB_D, status="completed", updated=str(-5 * 3600),
             turns=_turns(finished_off=-5 * 3600))
    # chat sess-e: nothing running, one unacknowledged failure -> the harness
    # acknowledges it there and expects the badge to disappear
    make_job(root, JOB_E, status="failed", updated=str(-7200), error="boom",
             origin={"platform": "webui", "chat_id": "sess-e", "ui_session_id": "sess-e"},
             turns=_turns(finished_off=-7200, exit_code=1))

    snapshot = tmp_path / "status.json"
    data = run_writer(tmp_path, root)
    assert snapshot.is_file()
    by_id = {j["job_id"]: j for j in data["jobs"]}
    assert (by_id[JOB_OK]["group"], by_id[JOB_B]["group"], by_id[JOB_C]["group"],
            by_id[JOB_D]["group"]) == ("active", "recent", "recent", "quiet")

    r = subprocess.run(
        [node, str(HERE / "js" / "smoke.js"), str(SRC / "assets" / "pi-lamp.js"), str(snapshot)],
        capture_output=True, text=True, timeout=300, cwd=str(SRC))
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, out
    assert "FAIL" not in out, out
    assert "passed" in out, out
