import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FAKE_PI = REPO / "tests" / "fake_pi.py"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated bridge home; force detached launcher for determinism.

    HOME is also redirected into the sandbox so that the fake `pi` writes
    its sessions to a private standard-store (~/.pi/agent/sessions) and the
    real user store / PI WEB config are never touched by tests.
    """
    home = tmp_path / "bridge-home"
    home.mkdir()
    pi_home = tmp_path / "fake-home"
    pi_home.mkdir()
    monkeypatch.setenv("PI_BRIDGE_HOME", str(home))
    monkeypatch.setenv("PI_BRIDGE_LAUNCHER", "detach")
    monkeypatch.setenv("HOME", str(pi_home))
    # No PI WEB config in the sandbox -> only the home directory is "allowed",
    # sandbox cwds are outside it and observability stays a no-op.
    monkeypatch.delenv("PI_WEB_URL", raising=False)
    monkeypatch.delenv("PI_WEB_CONFIG", raising=False)
    return {"home": home, "pi_home": pi_home, "tmp": tmp_path}


@pytest.fixture()
def workdir(tmp_path):
    d = tmp_path / "work dir with spaces"
    d.mkdir()
    return d


def bridge(*args, input=None, check=None, launcher=None, extra_env=None):
    env = os.environ.copy()
    if launcher:
        env["PI_BRIDGE_LAUNCHER"] = launcher
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(
        [sys.executable, "-m", "pi_bridge", *args],
        input=input, capture_output=True, text=True, env=env,
        cwd=REPO, timeout=60,
    )
    if check is not None:
        assert r.returncode == check, f"rc={r.returncode} out={r.stdout} err={r.stderr}"
    return r


def submit(env, workdir, task="do the thing", **kw):
    r = bridge("submit", "--cwd", str(workdir), "--task", task,
               "--pi-bin", str(FAKE_PI), "--json", check=0)
    return json.loads(r.stdout)


def wait_terminal(job_id, timeout=20, extra_env=None):
    """Poll status until terminal; returns parsed status view."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = bridge("status", job_id, "--json", check=0, extra_env=extra_env)
        last = json.loads(r.stdout)
        if last["status"] not in ("queued", "running"):
            return last
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} not terminal after {timeout}s: {last}")


def job_json(home, job_id):
    with open(Path(home) / "jobs" / job_id / "job.json") as f:
        return json.load(f)
