"""Five isolated bridge regression contracts.

These exercise the CURRENT bridge runner / delivery / wake implementation.
Pi is scripted in the deterministic tier. They are NOT an Attest LLM cassette
replay and must not be used as evidence of real Pi coding ability.
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from conftest import FAKE_PI, bridge, job_json, submit, wait_terminal
from fake_webhook import start_receiver

SESSION = "attest_sandbox_session"
ORIGIN = ("--origin-platform", "webui", "--origin-ui-session-id", SESSION,
          "--origin-chat-id", SESSION)
SECRET = "attest-local-test-secret"
FAST = {"PI_BRIDGE_WAKE_INTERVAL": "0.2",
        "PI_BRIDGE_WAKE_MAX_ATTEMPTS": "35",
        "PI_BRIDGE_WAKE_HTTP_TIMEOUT": "0.5"}


def _wait_until(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        item = predicate()
        if item:
            return item
        time.sleep(0.15)
    raise AssertionError("regression contract timed out")


def _status(job_id):
    return json.loads(bridge("status", job_id, "--json", check=0).stdout)


def _spool(env, job_id):
    file = env["home"] / "jobs" / job_id / "webui_delivery.json"
    if not file.exists():
        return None
    return json.loads(file.read_text())


def _scripted_editor(tmp_path):
    """A separate fake Pi that changes files, then delegates to existing fake Pi.

    The sandbox still exercises the real bridge's spawn, status, and delivery
    code; only the agent-side editing/LLM decision is scripted.
    """
    executable = tmp_path / "scripted-pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import subprocess,sys\n"
        "task = sys.stdin.read()\n"
        "cwd = Path.cwd()\n"
        "if 'WRITE_SENTINEL' in task:\n"
        "    (cwd / 'sentinel.txt').write_text('edited by Pi\\n')\n"
        "if 'FIX_CALC' in task:\n"
        "    (cwd / 'calc.py').write_text('def add(a, b):\\n    return a + b\\n')\n"
        f"result = subprocess.run([sys.executable, {str(FAKE_PI)!r}, *sys.argv[1:]], input=task, text=True)\n"
        "sys.exit(result.returncode)\n"
    )
    executable.chmod(0o700)
    return executable


def _submit_editor(workdir, executable, task, *extra):
    response = bridge("submit", "--cwd", str(workdir), "--pi-bin",
                      str(executable), "--task", task, "--json", *extra,
                      check=0)
    return json.loads(response.stdout)


def test_01_delegate_file_edit_and_return(env, workdir, tmp_path):
    pi = _scripted_editor(tmp_path)
    view = _submit_editor(workdir, pi, "WRITE_SENTINEL", *ORIGIN)
    result = wait_terminal(view["job_id"])
    assert result["status"] == "completed", result
    assert (workdir / "sentinel.txt").read_text() == "edited by Pi\n"
    spooled = _wait_until(lambda: _spool(env, view["job_id"]))
    assert spooled["job_id"] == view["job_id"]
    assert spooled["ui_session_id"] == SESSION
    assert spooled["state"] == "pending"
    assert "FAKEOK" in spooled["text"]

    # Simulate the WebUI-hosted Hermes plugin consuming its durable event.
    from pi_bridge import webui_event
    request = webui_event.read_request(env["home"] / "jobs" / view["job_id"])
    claimed = webui_event.claim(request)
    assert claimed is not None
    event = webui_event.build_event(claimed)
    assert event["origin_ui_session_id"] == SESSION
    assert event["delegation_id"] == webui_event.delegation_id(view["job_id"], 1)
    webui_event.release(claimed, "delivered", reason="ok")
    assert _spool(env, view["job_id"])["state"] == "delivered"
    assert len(job_json(env["home"], view["job_id"])["turns"]) == 1


def test_02_long_job_eventually_delivered(env, workdir):
    receiver = start_receiver(secret=SECRET)
    try:
        url = f"http://127.0.0.1:{receiver.server_address[1]}/webhooks/pi-bridge-complete"
        (env["home"] / "wake.json").write_text(json.dumps(
            {"enabled": True, "url": url, "secret": SECRET,
             "origin_delivery": False}))
        response = bridge("submit", "--cwd", str(workdir), "--pi-bin",
                          str(FAKE_PI), "--task", "BRIDGE_SLEEP 1.2",
                          "--json", extra_env=FAST, check=0)
        job = json.loads(response.stdout)
        final = wait_terminal(job["job_id"], timeout=20)
        assert final["status"] == "completed"
        assert final["final_result"].startswith("FAKEOK")
        _wait_until(lambda: _status(job["job_id"])["wake"]["delivered"])
        assert len(receiver.events) == 1
        assert receiver.events[0]["body"]["job_id"] == job["job_id"]
        assert len(job_json(env["home"], job["job_id"])["turns"]) == 1
    finally:
        receiver.shutdown()
        receiver.server_close()


def test_03_gateway_downtime_and_recovery(env, workdir):
    # Simulate gateway going away, then returning at exactly its old port.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}/webhooks/pi-bridge-complete"
    (env["home"] / "wake.json").write_text(json.dumps(
        {"enabled": True, "url": url, "secret": SECRET,
         "origin_delivery": False}))
    response = bridge("submit", "--cwd", str(workdir), "--pi-bin",
                      str(FAKE_PI), "--task", "gateway recovers",
                      "--json", extra_env=FAST, check=0)
    job_id = json.loads(response.stdout)["job_id"]
    final = wait_terminal(job_id)
    assert final["status"] == "completed"
    _wait_until(lambda: _status(job_id)["wake"]["attempts"] >= 1)
    receiver = start_receiver(port=port, secret=SECRET)
    try:
        _wait_until(lambda: _status(job_id)["wake"]["delivered"], timeout=15)
        assert len(receiver.events) == 1, receiver.events
        assert receiver.events[0]["verified"] is True
        assert receiver.events[0]["body"]["job_id"] == job_id
        assert len(job_json(env["home"], job_id)["turns"]) == 1
    finally:
        receiver.shutdown()
        receiver.server_close()


def test_04_pi_failure_bounded_no_loop(env, workdir):
    response = bridge("submit", "--cwd", str(workdir), "--pi-bin",
                      str(FAKE_PI), "--task", "BRIDGE_FAIL",
                      "--json", *ORIGIN, check=0)
    job_id = json.loads(response.stdout)["job_id"]
    final = wait_terminal(job_id)
    assert final["status"] == "failed"
    assert "simulated model error" in final["error"]
    spooled = _wait_until(lambda: _spool(env, job_id))
    assert spooled["status"] == "failed"
    assert spooled["state"] == "pending"
    assert "simulated model error" in spooled["text"]
    assert len(job_json(env["home"], job_id)["turns"]) == 1
    assert not list(env["home"].glob("jobs/*/turns/02.task.txt"))


def test_05_coding_changes_repo_and_tests_pass(env, tmp_path):
    repo = tmp_path / "isolated-git-repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "calc.py", "test_calc.py"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm",
                    "initial broken calculation"], check=True)
    editor = _scripted_editor(tmp_path)
    job = _submit_editor(repo, editor, "FIX_CALC")
    final = wait_terminal(job["job_id"])
    assert final["status"] == "completed", final
    change = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--name-only"], text=True)
    assert change.strip() == "calc.py", change
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "test_calc.py"],
                         cwd=repo, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(job_json(env["home"], job["job_id"])["turns"]) == 1
