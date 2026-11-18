"""Durable FIFO job queue.

Covers the acceptance points of the queue feature:
* strict FIFO inside a class, one heavy slot by default;
* the separate light/urgent slot (a light job does not wait behind a heavy
  one, and never consumes the heavy slot);
* durability: the queue is ONLY job.json state, so a waiting job survives a
  runner/gateway process death and is admitted by a fresh process;
* backward compatibility: submit without a flag is a heavy job, a job.json
  written before the queue keeps working, and PI_BRIDGE_QUEUE=0 restores the
  pre-queue "launch immediately" behaviour.
"""
import json
import os
import signal
import time
from pathlib import Path

from conftest import FAKE_PI, bridge, job_json, wait_terminal

FAKE = ["--pi-bin", str(FAKE_PI)]


def _submit(workdir, task, extra=None, light=False, args=None):
    argv = ["submit", "--cwd", str(workdir), "--task", task, *FAKE, "--json"]
    if light:
        argv.append("--light")
    argv += list(args or [])
    r = bridge(*argv, extra_env=extra)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def _status(job_id, extra=None):
    r = bridge("status", job_id, "--json", check=0, extra_env=extra)
    return json.loads(r.stdout)


def _set_cfg(home, cfg):
    (Path(home) / "config.json").write_text(json.dumps(cfg))


def _wait_state(job_id, want, timeout=25, extra=None):
    """Poll until status_view matches `want` (status or (status, queue state))."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        v = _status(job_id, extra)
        last = v
        if isinstance(want, str):
            if v["status"] == want:
                return v
        else:
            st, qs = want
            if v["status"] == st and v["queue"]["state"] == qs:
                return v
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} never reached {want}: {last}")


def _kill_runner_group(job_id, extra=None):
    """Kill the runner process group of a job (simulates a crashed runner /
    restarted gateway: the runner is gone, pi with it, state stays on disk)."""
    v = _status(job_id, extra)
    pid = v["runner_pid"]
    assert pid, f"job {job_id} has no runner pid: {v}"
    os.killpg(os.getpgid(pid), signal.SIGKILL)


# --------------------------------------------------------------------------
# FIFO + slots
# --------------------------------------------------------------------------

def test_fifo_order_is_exact_within_the_same_second(env, workdir):
    """Regression: `created_at` is second-resolution and job ids end in random
    hex, so ordering by those would silently shuffle jobs submitted inside one
    second. The queue orders by its own nanosecond enqueue key."""
    _set_cfg(env["home"], {"queue": {"heavy_slots": 0}})
    ids = [_submit(workdir, f"job {i}")["job_id"] for i in range(4)]
    for pos, job_id in enumerate(ids, 1):
        v = _status(job_id)
        assert v["queue"]["state"] == "waiting"
        assert v["queue"]["position"] == pos, (job_id, pos, v["queue"])


def test_default_queue_serializes_heavy_jobs_in_fifo_order(env, workdir):
    a = _submit(workdir, "first BRIDGE_SLEEP 2.5")
    _wait_state(a["job_id"], "running")
    b = _submit(workdir, "second BRIDGE_SLEEP 2.5")
    c = _submit(workdir, "third")
    # the two later jobs wait: queued, no runner was ever launched
    for j in (b, c):
        assert j["status"] == "queued"
        assert j["queue"]["state"] == "waiting"
        job = job_json(env["home"], j["job_id"])
        assert job["last_run"] is None, "waiting job must not have a runner"
        assert job["queue"]["state"] == "waiting"
    # ...and they are second and third in line, not first
    assert b["queue"]["position"] == 1
    assert c["queue"]["position"] == 2

    fa = wait_terminal(a["job_id"])
    assert fa["status"] == "completed"
    # the finishing runner's own pump admits the next job: FIFO, so B first --
    # while B holds the single heavy slot, C must still be waiting
    _wait_state(b["job_id"], "running", timeout=15)
    vc = _status(c["job_id"])
    assert (vc["status"], vc["queue"]["state"]) == ("queued", "waiting"), vc

    fb = wait_terminal(b["job_id"])
    assert fb["status"] == "completed"
    fc = wait_terminal(c["job_id"])
    assert fc["status"] == "completed"
    jb, jc = job_json(env["home"], b["job_id"]), job_json(env["home"], c["job_id"])
    assert jb["queue"]["started_at"] <= jc["queue"]["started_at"]
    # C's runner only ever started after B's turn closed
    assert jb["turns"][0]["finished_at"] <= jc["last_run"]["started_at"]


def test_light_slot_runs_alongside_heavy_and_light_jobs_queue_among_themselves(
        env, workdir):
    heavy = _submit(workdir, "heavy BRIDGE_SLEEP 2.5")
    _wait_state(heavy["job_id"], "running")
    light = _submit(workdir, "urgent BRIDGE_SLEEP 2.5", light=True)
    # the light job does NOT wait behind the heavy one: separate slot
    _wait_state(light["job_id"], "running", timeout=10)
    # the heavy slot is taken by the heavy job and the light slot by the
    # light job: another light job and another heavy job both wait
    light2 = _submit(workdir, "urgent 2", light=True)
    heavy2 = _submit(workdir, "heavy 2")
    assert light2["queue"]["state"] == "waiting"
    assert light2["queue"]["class"] == "light"
    assert light2["queue"]["position"] == 1
    assert heavy2["queue"]["state"] == "waiting"
    assert heavy2["queue"]["class"] == "heavy"

    assert wait_terminal(heavy["job_id"])["status"] == "completed"
    assert wait_terminal(light["job_id"])["status"] == "completed"
    # now the freed slots take both waiters (one per class)
    assert wait_terminal(light2["job_id"])["status"] == "completed"
    assert wait_terminal(heavy2["job_id"])["status"] == "completed"


def test_slot_budget_is_configurable(env, workdir):
    _set_cfg(env["home"], {"queue": {"heavy_slots": 2, "light_slots": 0}})
    a = _submit(workdir, "A BRIDGE_SLEEP 2")
    b = _submit(workdir, "B BRIDGE_SLEEP 2")
    _wait_state(a["job_id"], "running")
    _wait_state(b["job_id"], "running", timeout=10)  # two heavy slots
    c = _submit(workdir, "C")
    assert c["queue"]["state"] == "waiting"
    # light_slots=0: a light job never starts while its budget is zero
    light = _submit(workdir, "L", light=True)
    assert light["queue"]["state"] == "waiting"
    assert light["queue"]["position"] == 1

    assert wait_terminal(a["job_id"])["status"] == "completed"
    assert wait_terminal(b["job_id"])["status"] == "completed"
    assert wait_terminal(c["job_id"])["status"] == "completed"
    # still waiting: nothing freed a light slot
    assert _status(light["job_id"])["queue"]["state"] == "waiting"
    # capacity is live: raise the light budget and pump again
    _set_cfg(env["home"], {"queue": {"heavy_slots": 2, "light_slots": 1}})
    r = bridge("queue", "pump", "--json", check=0)
    assert [s["job_id"] for s in json.loads(r.stdout)["started"]] == [light["job_id"]]
    assert wait_terminal(light["job_id"])["status"] == "completed"


def test_env_overrides_config(env, workdir):
    _set_cfg(env["home"], {"queue": {"heavy_slots": 1}})
    extra = {"PI_BRIDGE_QUEUE_HEAVY_SLOTS": "2"}
    a = _submit(workdir, "A BRIDGE_SLEEP 2", extra=extra)
    b = _submit(workdir, "B BRIDGE_SLEEP 2", extra=extra)
    _wait_state(a["job_id"], "running", extra=extra)
    _wait_state(b["job_id"], "running", timeout=10, extra=extra)
    for j in (a, b):
        assert wait_terminal(j["job_id"], extra_env=extra)["status"] == "completed"


# --------------------------------------------------------------------------
# durability
# --------------------------------------------------------------------------

def test_waiting_job_is_pure_disk_state_and_is_admitted_by_a_fresh_process(
        env, workdir):
    """Kill the runner of the running job; a brand new bridge process (a new
    `queue pump`, exactly what the watchdog timer runs after a restart) must
    reconcile the dead job and admit the waiter from job.json alone."""
    a = _submit(workdir, "A BRIDGE_SLEEP 30")
    _wait_state(a["job_id"], "running")
    b = _submit(workdir, "B")
    assert b["queue"]["state"] == "waiting"

    _kill_runner_group(a["job_id"])
    # The runner is gone without a terminal status; reconcile owns it, and
    # it is deliberately conservative for the first STARTUP_GRACE seconds.
    time.sleep(11)
    va = _status(a["job_id"])
    assert va["status"] == "interrupted", va
    # A fresh process admits the waiter using nothing but the job directory.
    r = bridge("queue", "pump", "--json", check=0)
    out = json.loads(r.stdout)
    assert [s["job_id"] for s in out["started"]] == [b["job_id"]]
    assert wait_terminal(b["job_id"])["status"] == "completed"


def test_reconcile_never_interrupts_a_waiting_job(env, workdir):
    """A waiting job has no runner BY DESIGN; lazy reconcile (pi_status path)
    must not turn it into `interrupted`."""
    _set_cfg(env["home"], {"queue": {"heavy_slots": 0, "light_slots": 0}})
    j = _submit(workdir, "waits for capacity")
    assert j["queue"]["state"] == "waiting"
    # older than STARTUP_GRACE, still just waiting
    p = Path(env["home"]) / "jobs" / j["job_id"] / "job.json"
    job = json.loads(p.read_text())
    job["created_at"] = "2026-01-01T00:00:00Z"
    p.write_text(json.dumps(job))
    for _ in range(3):
        v = _status(j["job_id"])
        assert v["status"] == "queued", v
        assert v["queue"]["state"] == "waiting"
        time.sleep(0.3)
    # cancelling a waiter is a plain state transition, not a kill
    r = bridge("cancel", j["job_id"], "--json", check=0)
    assert json.loads(r.stdout)["status"] == "cancelled"
    assert not (Path(env["home"]) / "jobs" / j["job_id"] / "turns" / "01.out").exists()


# --------------------------------------------------------------------------
# backward compatibility
# --------------------------------------------------------------------------

def test_submit_without_flag_is_heavy_and_job_json_stays_readable(env, workdir):
    j = _submit(workdir, "plain job")
    assert j["queue"]["class"] == "heavy"
    job = job_json(env["home"], j["job_id"])
    # every pre-queue key is still there, unchanged
    for k in ("job_id", "created_at", "cwd", "status", "pi_session_id",
              "pi_bin", "task", "feedbacks", "turns", "last_run",
              "cancel_requested", "env_snapshot", "wake", "final_result"):
        assert k in job, k
    assert wait_terminal(j["job_id"])["status"] == "completed"


def test_legacy_job_json_without_queue_keys_is_heavy_and_holds_a_slot(tmp_path,
                                                                     monkeypatch):
    """A job.json written by bridge <= 0.5.0 (no job_class, no queue block)
    must classify as heavy, keep its slot and still be readable."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PI_BRIDGE_HOME", str(home))
    from pi_bridge import bridge as bridge_mod
    from pi_bridge import queue as queue_mod
    from pi_bridge import state

    job = {
        "job_id": "pb-20260101T000000Z-aaaaaa",
        "created_at": "2026-01-01T00:00:00Z",
        "cwd": str(tmp_path),
        "status": "running",
        "pi_session_id": "legacy-session",
        "pi_bin": "/bin/true",
        "task": "legacy",
        "feedbacks": [],
        "turns": [{"n": 1, "kind": "task", "started_at": "2026-01-01T00:00:00Z",
                   "finished_at": None, "exit_code": None, "pi_pid": None}],
        "last_run": {"launcher": "detach", "unit": None, "pid": os.getpid(),
                     "started_at": "2026-01-01T00:00:00Z"},
        "cancel_requested": False,
        "wake": {},
    }
    state.save_job(job)
    assert queue_mod.job_class(job) == "heavy"
    assert queue_mod.holds_slot(job) is True
    assert queue_mod.is_waiting(job) is False
    assert queue_mod.normalize_block(job)["state"] == "running"
    assert queue_mod.usage([job]) == {"heavy": 1, "light": 0}
    v = queue_mod.view(job)
    assert v["class"] == "heavy" and v["position"] is None
    s = queue_mod.summary([job])
    assert s["running"] == {"heavy": 1, "light": 0}
    assert s["queue"] == []


def test_queue_disabled_restores_immediate_launch(env, workdir):
    extra = {"PI_BRIDGE_QUEUE": "0"}
    a = _submit(workdir, "A BRIDGE_SLEEP 2", extra=extra)
    b = _submit(workdir, "B BRIDGE_SLEEP 2", extra=extra)
    # both start at once: no admission control at all, exactly like 0.5.0
    _wait_state(a["job_id"], "running", extra=extra)
    _wait_state(b["job_id"], "running", timeout=10, extra=extra)
    jb = job_json(env["home"], b["job_id"])
    assert jb["last_run"], "submit without the queue anchors the runner"
    for j in (a, b):
        assert wait_terminal(j["job_id"], extra_env=extra)["status"] == "completed"


def test_queue_status_cli_reports_slots(env, workdir):
    _set_cfg(env["home"], {"queue": {"heavy_slots": 1, "light_slots": 1}})
    a = _submit(workdir, "A BRIDGE_SLEEP 2")
    _wait_state(a["job_id"], "running")
    b = _submit(workdir, "B")
    r = bridge("queue", "status", "--json", check=0)
    s = json.loads(r.stdout)
    assert s["enabled"] is True
    assert s["slots"] == {"heavy": 1, "light": 1}
    assert s["running"] == {"heavy": 1, "light": 0}
    assert s["waiting"] == {"heavy": 1, "light": 0}
    assert [q["job_id"] for q in s["queue"]] == [b["job_id"]]
    assert s["queue"][0]["class"] == "heavy"
    assert a["job_id"] in [j["job_id"] for j in s["running_jobs"]]
    assert wait_terminal(a["job_id"])["status"] == "completed"
    assert wait_terminal(b["job_id"])["status"] == "completed"
    # The persisted block is last written when the slot is claimed, so a
    # finished job must not be reported as holding one (`running` forever).
    rows = {r["job_id"]: r for r in json.loads(bridge("list", "--json").stdout)}
    for job_id in (a["job_id"], b["job_id"]):
        assert rows[job_id]["queue"]["state"] == "finished", rows[job_id]
        assert rows[job_id]["queue"]["class"] == "heavy"
        assert _status(job_id)["queue"]["state"] == "finished"


def test_feedback_reenters_the_queue(env, workdir):
    """A feedback turn is a new execution: it needs a slot like any other."""
    _set_cfg(env["home"], {"queue": {"heavy_slots": 1, "light_slots": 0}})
    a = _submit(workdir, "A")
    assert wait_terminal(a["job_id"])["status"] == "completed"
    b = _submit(workdir, "B BRIDGE_SLEEP 2.5")
    _wait_state(b["job_id"], "running")
    r = bridge("feedback", a["job_id"], "--json",
               input="more work please", check=0)
    v = json.loads(r.stdout)
    assert v["status"] == "queued"
    assert v["queue"]["state"] == "waiting"
    assert v["queue"]["class"] == "heavy"
    assert wait_terminal(a["job_id"])["status"] == "completed"
    assert len(job_json(env["home"], a["job_id"])["turns"]) == 2
