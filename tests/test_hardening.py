"""Additional hardening tests: pi child termination, systemd cancel,
orphan-pi protection, and the Hermes plugin handler contract."""
import importlib.util
import json
import os
import signal
import time
from pathlib import Path

from conftest import FAKE_PI, REPO, bridge, job_json, submit, wait_terminal

from test_bridge import wait_running


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_cancel_also_kills_pi_child(env, workdir):
    view = submit(env, workdir, task="BRIDGE_SLEEP 120")
    wait_running(view["job_id"])
    job = job_json(env["home"], view["job_id"])
    pi_pid = job["turns"][0]["pi_pid"]
    assert pi_pid and _pid_alive(pi_pid)
    r = bridge("cancel", view["job_id"], "--json", check=0)
    assert json.loads(r.stdout)["status"] == "cancelled"
    # the pi child must be gone, not orphaned
    deadline = time.time() + 5
    while _pid_alive(pi_pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _pid_alive(pi_pid)


def test_systemd_cancel(env, workdir):
    if not os.path.exists("/run/user/%d/systemd" % os.getuid()):
        import pytest

        pytest.skip("no user systemd instance")
    r = bridge("submit", "--cwd", str(workdir), "--pi-bin", str(FAKE_PI),
               "--task", "BRIDGE_SLEEP 120", "--json", check=0,
               launcher="systemd")
    view = json.loads(r.stdout)
    assert view["launcher"] == "systemd"
    wait_running(view["job_id"])
    job = job_json(env["home"], view["job_id"])
    assert job["last_run"]["unit"] == f"pi-bridge-{view['job_id']}-t1"
    pi_pid = job["turns"][0]["pi_pid"]
    out = bridge("cancel", view["job_id"], "--json", check=0)
    assert json.loads(out.stdout)["status"] == "cancelled"
    if pi_pid:
        deadline = time.time() + 8
        while _pid_alive(pi_pid) and time.time() < deadline:
            time.sleep(0.2)
        assert not _pid_alive(pi_pid)


def test_feedback_refused_while_previous_pi_alive(env, workdir):
    """Crash the runner but leave pi running: feedback must not start a
    second pi on the same session."""
    view = submit(env, workdir, task="BRIDGE_SLEEP 120")
    wait_running(view["job_id"])
    job = job_json(env["home"], view["job_id"])
    pi_pid = job["turns"][0]["pi_pid"]
    os.kill(job["last_run"]["pid"], signal.SIGKILL)  # runner dies, pi orphaned
    try:
        # orphan pi is still alive while the job reconciles to interrupted
        deadline = time.time() + 20
        v = None
        while time.time() < deadline:
            v = json.loads(bridge("status", view["job_id"], "--json",
                                  check=0).stdout)
            if v["status"] == "interrupted":
                break
            time.sleep(0.3)
        assert v["status"] == "interrupted"
        r = bridge("feedback", view["job_id"], "--feedback", "nope", "--json")
        assert r.returncode != 0
        assert "still" in json.loads(r.stderr)["error"]
    finally:
        try:
            os.kill(pi_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_feedback_after_old_turn_not_false_interrupted(env, workdir):
    """Regression: feedback on a job whose previous turn ended > grace
    ago must not be reconciled to 'interrupted' via a stale liveness anchor."""
    view = submit(env, workdir, task="turn one")
    wait_terminal(view["job_id"])
    time.sleep(11)  # older than STARTUP_GRACE (10s)
    bridge("feedback", view["job_id"], "--feedback", "turn two",
           "--json", check=0)
    # immediate status reads: must be queued/running/RESUMED-completed,
    # never interrupted, and feedback must be accepted exactly once
    seen = set()
    for _ in range(25):
        v = json.loads(bridge("status", view["job_id"], "--json",
                              check=0).stdout)
        seen.add(v["status"])
        assert v["status"] != "interrupted", v
        if v["status"] == "completed":
            break
        time.sleep(0.2)
    assert v["status"] == "completed"
    assert v["final_result"].startswith("RESUMED")


def test_orphan_guard_survives_cmdline_rewrite(env, workdir):
    """Real pi rewrites its own cmdline (process.title='pi'), so the orphan
    guard must rely on the recorded /proc start-time, not cmdline."""
    import subprocess as sp

    from pi_bridge.state import pid_start_ticks

    view = submit(env, workdir, task="quick one")
    wait_terminal(view["job_id"])
    p = sp.Popen(["/bin/sleep", "60"])  # cmdline contains no session id
    try:
        jp = Path(env["home"]) / "jobs" / view["job_id"] / "job.json"
        job = json.loads(jp.read_text())
        job["turns"][0]["pi_pid"] = p.pid
        job["turns"][0]["pi_start"] = pid_start_ticks(p.pid)
        assert job["turns"][0]["pi_start"] is not None
        jp.write_text(json.dumps(job))
        r = bridge("feedback", view["job_id"], "--feedback", "x", "--json")
        assert r.returncode != 0
        assert "still running" in json.loads(r.stderr)["error"]
        # and the guard does not misfire once that pid is really gone
        p.kill()
        p.wait()
        time.sleep(0.2)
        r2 = bridge("feedback", view["job_id"], "--feedback", "now ok",
                    "--json", check=0)
        assert json.loads(r2.stdout)["status"] in ("queued", "running")
        final = wait_terminal(view["job_id"])
        assert final["status"] == "completed"
    finally:
        try:
            p.kill()
        except ProcessLookupError:
            pass


def _load_plugin_module():
    path = REPO / "plugin" / "__init__.py"
    spec = importlib.util.spec_from_file_location("pi_worker_plugin_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_plugin_contract_and_e2e(env, workdir, monkeypatch):
    monkeypatch.setenv("PI_BRIDGE_CLI", str(REPO / ".venv" / "bin" / "pi-bridge"))
    monkeypatch.setenv("PI_BRIDGE_PI_BIN", str(FAKE_PI))
    monkeypatch.setenv("PI_BRIDGE_LAUNCHER", "detach")
    mod = _load_plugin_module()

    registered = {}

    class Ctx:
        def register_tool(self, name, toolset, schema, handler, **kw):
            registered[name] = handler
            assert callable(handler)

    mod.register(Ctx())
    assert sorted(registered) == ["pi_cancel", "pi_delegate", "pi_feedback",
                                  "pi_list", "pi_status"]

    # handler contract: bad args -> JSON error string, never raises
    for name, h in registered.items():
        out = h(None, task_id="x")
        assert isinstance(out, str)
        assert "error" in json.loads(out)

    # full lifecycle through the handlers
    out = json.loads(registered["pi_delegate"]({"task": "via plugin", "cwd": str(workdir)}))
    jid = out["job_id"]
    assert out["status"] in ("queued", "running")
    deadline = time.time() + 20
    while time.time() < deadline:
        v = json.loads(registered["pi_status"]({"job_id": jid}))
        if v["status"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    assert v["status"] == "completed"
    assert v["final_result"].startswith("FAKEOK")
    registered["pi_feedback"]({"job_id": jid, "feedback": "again"})
    while time.time() < deadline:
        v = json.loads(registered["pi_status"]({"job_id": jid}))
        if v["status"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    assert v["status"] == "completed"
    assert v["final_result"].startswith("RESUMED")
    # cancel of finished job -> structured error string
    err = json.loads(registered["pi_cancel"]({"job_id": jid}))
    assert "error" in err

    # missing CLI -> structured error, no raise
    monkeypatch.setenv("PI_BRIDGE_CLI", "/nonexistent/pi-bridge")
    assert "error" in json.loads(registered["pi_status"]({"job_id": "x"}))
