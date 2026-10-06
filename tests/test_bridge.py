"""Integration tests for pi-bridge using a fake `pi` executable."""
import json
import os
import time
from pathlib import Path

from conftest import FAKE_PI, bridge, job_json, submit, wait_terminal


def test_submit_is_fast_and_creates_state(env, workdir):
    t0 = time.time()
    view = submit(env, workdir, task="hello bridge")
    elapsed = time.time() - t0
    assert elapsed < 2.0, f"submit took {elapsed:.2f}s"
    assert view["status"] in ("queued", "running")
    job = job_json(env["home"], view["job_id"])
    assert job["cwd"] == str(workdir.resolve())
    assert job["task"] == "hello bridge"
    assert job["pi_session_id"]
    assert (env["home"] / "jobs" / view["job_id"] / "job.json").is_file()


def test_successful_completion(env, workdir):
    view = submit(env, workdir, task="hello bridge")
    final = wait_terminal(view["job_id"])
    assert final["status"] == "completed"
    assert final["final_result"].startswith("FAKEOK")
    assert "hello bridge" not in final["final_result"]  # result is pi output only
    job = job_json(env["home"], view["job_id"])
    assert job["turns"][0]["exit_code"] == 0
    assert job["pi_session_file"] and Path(job["pi_session_file"]).is_file()
    # V1.1: the runner passes no --session-dir, so pi's main session lands in
    # its standard store (the store PI WEB lists), never inside the job dir.
    assert Path(job["pi_session_file"]).is_relative_to(env["pi_home"] / ".pi")
    assert not list((env["home"] / "jobs" / view["job_id"] / "sessions")
                    .rglob("*.jsonl"))
    assert (env["home"] / "jobs" / view["job_id"] / "final_result.md").is_file()


def test_failure_nonzero_exit(env, workdir):
    view = submit(env, workdir, task="BRIDGE_FAIL please")
    final = wait_terminal(view["job_id"])
    assert final["status"] == "failed"
    assert "exited with code 3" in final["error"]
    assert "simulated model error" in final["error"]
    assert final["final_result"] is None


def test_status_reports_running(env, workdir):
    view = submit(env, workdir, task="BRIDGE_SLEEP 6")
    try:
        time.sleep(1.0)
        r = bridge("status", view["job_id"], "--json", check=0)
        v = json.loads(r.stdout)
        assert v["status"] == "running"
        assert v["runner_alive"] is True
    finally:
        bridge("cancel", view["job_id"], "--json", check=0)


def test_cancel_stops_running_job(env, workdir):
    view = submit(env, workdir, task="BRIDGE_SLEEP 120")
    wait_running(view["job_id"])
    r = bridge("cancel", view["job_id"], "--json", check=0)
    v = json.loads(r.stdout)
    assert v["status"] == "cancelled"
    assert v["runner_alive"] is False
    job = job_json(env["home"], view["job_id"])
    assert job["status"] == "cancelled"
    # second cancel -> structured error, non-zero exit
    r2 = bridge("cancel", view["job_id"], "--json")
    assert r2.returncode != 0
    assert "error" in json.loads(r2.stderr)


def wait_running(job_id):
    deadline = time.time() + 10
    while time.time() < deadline:
        r = bridge("status", job_id, "--json", check=0)
        if json.loads(r.stdout)["status"] == "running":
            return
        time.sleep(0.2)
    raise AssertionError("never reached running")


def test_cancel_unknown_job_is_refused(env, workdir):
    r = bridge("cancel", "pb-20200101T000000Z-abcdef", "--json")
    assert r.returncode != 0
    assert "unknown job id" in json.loads(r.stderr)["error"]
    r = bridge("cancel", "../../etc", "--json")
    assert r.returncode != 0  # invalid job id pattern refused


def test_feedback_same_session(env, workdir):
    view = submit(env, workdir, task="first task")
    first = wait_terminal(view["job_id"])
    assert first["status"] == "completed"

    r = bridge("feedback", view["job_id"], "--feedback", "second turn",
               "--json", check=0)
    v = json.loads(r.stdout)
    assert v["status"] in ("queued", "running")
    second = wait_terminal(view["job_id"])
    assert second["status"] == "completed"
    # fake pi prints RESUMED when it found an existing session for the SAME
    # session id in the same session dir + cwd: proves session continuation.
    assert second["final_result"].startswith(f"RESUMED sid={v['pi_session_id']}")
    job = job_json(env["home"], view["job_id"])
    assert job["pi_session_id"] == v["pi_session_id"]
    assert len(job["feedbacks"]) == 1
    assert job["feedbacks"][0]["text"] == "second turn"
    # still exactly one session file
    # standard Pi session store (HOME sandboxed by the env fixture):
    # still exactly one session file, two turns in it
    sessions = list((env["pi_home"] / ".pi" / "agent" / "sessions").rglob("*.jsonl"))
    assert len(sessions) == 1
    assert len(sessions[0].read_text().strip().splitlines()) == 2


def test_feedback_refused_while_running(env, workdir):
    view = submit(env, workdir, task="BRIDGE_SLEEP 120")
    wait_running(view["job_id"])
    try:
        r = bridge("feedback", view["job_id"], "--feedback", "nope", "--json")
        assert r.returncode != 0
        assert "only accepted on terminal" in json.loads(r.stderr)["error"]
    finally:
        bridge("cancel", view["job_id"], check=0)


def test_cwd_with_spaces(env, tmp_path):
    weird = tmp_path / "dir with spaces & (parens)"
    weird.mkdir()
    view = submit(env, weird, task="task in weird dir")
    final = wait_terminal(view["job_id"])
    assert final["status"] == "completed"
    assert final["cwd"] == str(weird.resolve())


def test_task_injection_safe(env, workdir):
    marker = env["tmp"] / "pwned-by-shell"
    task = (f"BRIDGE_ECHO_TASK $(touch {marker}) ; `touch {marker}2` "
            f"&& touch {marker}3\nnewlines \"and\" 'quotes' \\ backslash")
    r = bridge("submit", "--cwd", str(workdir), "--pi-bin", str(FAKE_PI),
               "--json", input=task, check=0)
    view = json.loads(r.stdout)
    final = wait_terminal(view["job_id"])
    assert final["status"] == "completed"
    # no shell was ever involved: nothing got executed
    assert not marker.exists()
    assert not marker.with_name("pwned-by-shell2").exists()
    assert not marker.with_name("pwned-by-shell3").exists()
    # task bytes survived byte-exact through stdin
    body = final["final_result"].split("TASK-BEGIN[", 1)[1].split("]TASK-END", 1)[0]
    assert body == task.replace("BRIDGE_ECHO_TASK", "")
    job = job_json(env["home"], view["job_id"])
    assert job["task"] == task


def test_parallel_jobs_do_not_mix(env, tmp_path):
    w1 = tmp_path / "w1"
    w2 = tmp_path / "w2"
    w1.mkdir()
    w2.mkdir()
    a = json.loads(bridge("submit", "--cwd", str(w1), "--pi-bin", str(FAKE_PI),
                          "--task", "BRIDGE_SLEEP 1 job-A-marker", "--json",
                          check=0).stdout)
    b = json.loads(bridge("submit", "--cwd", str(w2), "--pi-bin", str(FAKE_PI),
                          "--task", "job-B-marker", "--json", check=0).stdout)
    assert a["job_id"] != b["job_id"]
    assert a["pi_session_id"] != b["pi_session_id"]
    fa = wait_terminal(a["job_id"])
    fb = wait_terminal(b["job_id"])
    assert fa["status"] == fb["status"] == "completed"
    ja = job_json(env["home"], a["job_id"])
    jb = job_json(env["home"], b["job_id"])
    assert ja["pi_session_id"] in fa["final_result"]
    assert jb["pi_session_id"] in fb["final_result"]
    assert ja["pi_session_file"] != jb["pi_session_file"]


def test_oversized_output_truncated(env, workdir):
    view = submit(env, workdir, task="BRIDGE_BIG")
    final = wait_terminal(view["job_id"])
    assert final["status"] == "completed"
    big = 1024 * 1024
    assert final["final_result_chars"] == big
    assert final["final_result_truncated"] is True
    assert len(final["final_result"]) <= 4000  # status view limit
    job = job_json(env["home"], view["job_id"])
    assert len(job["final_result"]) <= 8000     # job.json limit
    full = (env["home"] / "jobs" / view["job_id"] / "final_result.md").read_text()
    assert len(full) == big


def test_interrupted_detection(env, workdir):
    view = submit(env, workdir, task="BRIDGE_SLEEP 120")
    wait_running(view["job_id"])
    job = job_json(env["home"], view["job_id"])
    pid = job["last_run"]["pid"]
    import signal

    os.kill(pid, signal.SIGKILL)  # simulate hard crash of the runner
    deadline = time.time() + 15
    while time.time() < deadline:
        v = json.loads(bridge("status", view["job_id"], "--json", check=0).stdout)
        if v["status"] == "interrupted":
            break
        time.sleep(0.3)
    assert v["status"] == "interrupted"


def test_list_jobs(env, workdir):
    view = submit(env, workdir, task="listable")
    wait_terminal(view["job_id"])
    r = bridge("list", "--json", check=0)
    rows = json.loads(r.stdout)
    assert any(x["job_id"] == view["job_id"] and x["status"] == "completed"
               for x in rows)


def test_systemd_launcher_end_to_end(env, workdir):
    if not os.path.exists("/run/user/%d/systemd" % os.getuid()):
        import pytest

        pytest.skip("no user systemd instance")
    r = bridge("submit", "--cwd", str(workdir), "--pi-bin", str(FAKE_PI),
               "--task", "systemd mode job", "--json", check=0, launcher="systemd")
    view = json.loads(r.stdout)
    assert view["launcher"] == "systemd"
    assert view["unit"] and view["unit"].startswith("pi-bridge-")
    final = wait_terminal(view["job_id"], timeout=30)
    assert final["status"] == "completed"
    assert final["final_result"].startswith("FAKEOK")
