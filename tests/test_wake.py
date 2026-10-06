"""V1.2 wake-notifier + pi_list tests.

The wake channel is the Hermes gateway webhook platform.  Nothing here ever
touches the real gateway: deliveries go to a loopback receiver
(tests/fake_webhook.py) that verifies the signature with the gateway's own
generic HMAC V2 rule (hex HMAC-SHA256 over "<unix-seconds>.<raw body>",
replay window +-300 s), so a passing test proves wire compatibility.
"""
import base64
import hashlib
import hmac
import json
import os
import signal
import socket
import time

import pytest
from conftest import FAKE_PI, REPO, bridge, job_json, wait_terminal
from fake_webhook import start_receiver

SECRET = "wake-test-secret-abcdef"
WAKE_FAST = {"PI_BRIDGE_WAKE_INTERVAL": "0.3",
             "PI_BRIDGE_WAKE_MAX_ATTEMPTS": "40",
             "PI_BRIDGE_WAKE_HTTP_TIMEOUT": "2"}


def enable_wake(env, url, secret=SECRET, **extra):
    (env["home"] / "wake.json").write_text(json.dumps(
        {"enabled": True, "url": url, "secret": secret, **extra}))


def receiver_url(srv) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}/webhooks/pi-bridge-complete"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def submit(env, workdir, task, extra_env=None):
    e = dict(WAKE_FAST)
    e.update(extra_env or {})
    r = bridge("submit", "--cwd", str(workdir), "--pi-bin", str(FAKE_PI),
               "--task", task, "--json", check=0, extra_env=e)
    return json.loads(r.stdout)


def wait_events(srv, n=1, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(srv.events) >= n:
            return srv.events
        time.sleep(0.1)
    raise AssertionError(f"only {len(srv.events)}/{n} wake deliveries arrived; "
                         f"rejected={len(srv.rejected)}")


def wait_wake(job_id, delivered=True, timeout=25, extra_env=None):
    """Poll status until the wake bookkeeping reaches `delivered`.

    The job goes terminal BEFORE the wake is recorded (that is the whole
    point), so tests must never read wake state from the first terminal
    snapshot.
    """
    deadline = time.time() + timeout
    v = None
    while time.time() < deadline:
        v = json.loads(bridge("status", job_id, "--json", check=0,
                              extra_env=extra_env or {}).stdout)
        if v["wake"]["delivered"] is delivered:
            return v
        time.sleep(0.2)
    raise AssertionError(f"wake.delivered never became {delivered}: {v}")


# --------------------------------------------------------------------------
# unit: configuration semantics (no subprocess)
# --------------------------------------------------------------------------

def test_forwarded_env_args_reach_the_systemd_runner(env, monkeypatch):
    """A transient systemd unit gets a clean env; the wake knobs must be
    forwarded explicitly or they would silently work only in detach mode."""
    from pi_bridge import bridge as b

    monkeypatch.setenv("PI_BRIDGE_WAKE_INTERVAL", "7")
    monkeypatch.setenv("PI_BRIDGE_WAKE_MAX_ATTEMPTS", "12")
    monkeypatch.delenv("PI_BRIDGE_WAKE_HTTP_TIMEOUT", raising=False)
    monkeypatch.setenv("PI_BRIDGE_WAKE_PERMANENT_AFTER", "bad\nvalue")
    args = b._forwarded_env_args()
    assert args[:2] == ["--setenv", "PI_BRIDGE_WAKE_INTERVAL=7"]
    assert "PI_BRIDGE_WAKE_MAX_ATTEMPTS=12" in args
    assert "PI_BRIDGE_WAKE_HTTP_TIMEOUT" not in " ".join(args)
    assert "PI_BRIDGE_WAKE_PERMANENT_AFTER" not in " ".join(args)
    # nothing secret is ever forwarded
    assert not [a for a in args if "SECRET" in a.upper()]


def test_wake_config_off_by_default(env, monkeypatch):
    from pi_bridge import wake

    monkeypatch.setenv("PI_BRIDGE_HOME", str(env["home"]))
    assert wake.load_config()["enabled"] is False
    assert wake.is_enabled() is False

    p = env["home"] / "wake.json"
    p.write_text("{ not json")
    assert wake.load_config()["enabled"] is False

    p.write_text(json.dumps({"enabled": False, "url": "http://x/y",
                             "secret": SECRET}))
    assert wake.load_config()["enabled"] is False

    p.write_text(json.dumps({"enabled": True, "url": "", "secret": SECRET}))
    assert wake.load_config()["enabled"] is False

    p.write_text(json.dumps({"enabled": True, "url": "http://127.0.0.1:1/x",
                             "secret": "  "}))
    assert wake.load_config()["enabled"] is False

    p.write_text(json.dumps({"enabled": True, "url": "ftp://127.0.0.1/x",
                             "secret": SECRET}))
    assert wake.load_config()["enabled"] is False

    p.write_text(json.dumps({"enabled": True, "url": "http://127.0.0.1:1/x",
                             "secret": SECRET}))
    cfg = wake.load_config()
    assert cfg["enabled"] is True and cfg["secret"] == SECRET


def test_wake_signing_matches_gateway_v2_scheme(env, monkeypatch):
    """Independent recomputation of the gateway's generic V2 signature."""
    from pi_bridge import wake

    monkeypatch.setenv("PI_BRIDGE_HOME", str(env["home"]))
    payload = {"event": wake.EVENT, "job_id": "pb-x", "status": "completed"}
    cfg = {"url": "http://127.0.0.1:1/x", "secret": SECRET}
    body, headers = wake.render_request(cfg, payload, "pb-x", 1)

    ts = headers["X-Webhook-Timestamp"]
    assert ts.isdigit()
    assert abs(int(time.time()) - int(ts)) <= 5
    # exactly what gateway/platforms/webhook.py:659 computes
    expected = hmac.new(SECRET.encode(), ts.encode() + b"." + body,
                        hashlib.sha256).hexdigest()
    assert headers["X-Webhook-Signature-V2"] == expected
    assert headers["Content-Type"] == "application/json"
    # constant across retries of the same turn -> idempotent delivery
    assert headers["X-Request-ID"] == wake.delivery_id("pb-x", 1)
    _, h2 = wake.render_request(cfg, payload, "pb-x", 1)
    assert h2["X-Request-ID"] == headers["X-Request-ID"]
    # ... while the timestamp/signature are re-computed per attempt
    assert body == json.dumps(payload, ensure_ascii=False,
                              separators=(",", ":")).encode()


# --------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------

def test_wake_delivered_on_completed(env, workdir):
    srv = start_receiver(secret=SECRET)
    try:
        enable_wake(env, receiver_url(srv))
        view = submit(env, workdir, task="wake me on completion")
        final = wait_terminal(view["job_id"], extra_env=WAKE_FAST)
        assert final["status"] == "completed"

        events = wait_events(srv)
        assert len(events) == 1
        ev = events[0]
        assert ev["verified"] is True, ev["verify_reason"]
        assert ev["other_scheme_headers"] == []
        assert ev["path"] == "/webhooks/pi-bridge-complete"
        p = ev["body"]
        assert p["event"] == "pi_bridge_turn_complete"
        # the gateway resolves the event type from `event_type` in the body,
        # so a route with events: [pi_bridge_turn_complete] must match
        assert p["event_type"] == "pi_bridge_turn_complete"
        assert p["job_id"] == view["job_id"]
        assert p["status"] == "completed"
        assert p["turn"] == 1
        assert p["cwd"] == str(workdir.resolve())
        assert p["pi_session_id"] == view["pi_session_id"]
        assert p["task_preview"].startswith("wake me on completion")
        assert "FAKEOK" in p["final_result_excerpt"]
        assert "error" not in p
        assert p["completed_at"].endswith("Z")
        # no transcript, no stderr, no secret anywhere in the payload
        assert SECRET not in json.dumps(p)
        assert "stderr" not in p and "transcript" not in p

        # the receiver's signature check is re-derived here from the RAW bytes
        raw = base64.b64decode(ev["raw_b64"])
        ts = ev["headers"]["X-Webhook-Timestamp"]
        assert hmac.new(SECRET.encode(), ts.encode() + b"." + raw,
                        hashlib.sha256).hexdigest() == \
            ev["headers"]["X-Webhook-Signature-V2"]
        assert abs(int(time.time()) - int(ts)) <= 300  # replay window

        final = wait_wake(view["job_id"], extra_env=WAKE_FAST)
        assert final["status"] == "completed"
        assert final["wake"] == {"enabled": True, "delivered": True,
                                 "attempts": 1, "last_error": None}
        job = job_json(env["home"], view["job_id"])
        assert job["wake"]["delivered"] is True
        # the secret never reaches job state or logs
        assert SECRET not in json.dumps(job)
        log = (env["home"] / "jobs" / view["job_id"] / "runner.log").read_text()
        assert SECRET not in log
        assert "wake t1: delivered" in log
    finally:
        srv.shutdown()


def test_wake_delivered_on_failed(env, workdir):
    srv = start_receiver(secret=SECRET)
    try:
        enable_wake(env, receiver_url(srv))
        view = submit(env, workdir, task="BRIDGE_FAIL must fail")
        final = wait_terminal(view["job_id"], extra_env=WAKE_FAST)
        assert final["status"] == "failed"

        ev = wait_events(srv)[0]
        assert ev["verified"] is True
        assert ev["body"]["status"] == "failed"
        assert ev["body"]["job_id"] == view["job_id"]
        assert "code 3" in ev["body"]["error"]
        assert wait_wake(view["job_id"], extra_env=WAKE_FAST)["wake"]["delivered"] is True
    finally:
        srv.shutdown()


def test_wake_delivered_on_feedback_turn(env, workdir):
    srv = start_receiver(secret=SECRET)
    try:
        enable_wake(env, receiver_url(srv))
        view = submit(env, workdir, task="first turn")
        wait_terminal(view["job_id"], extra_env=WAKE_FAST)
        wait_events(srv, 1)

        bridge("feedback", view["job_id"], "--feedback", "second turn please",
               "--json", check=0, extra_env=WAKE_FAST)
        final = wait_terminal(view["job_id"], extra_env=WAKE_FAST)
        assert final["status"] == "completed"

        events = wait_events(srv, 2)
        assert [e["body"]["turn"] for e in events] == [1, 2]
        assert [e["body"]["status"] for e in events] == ["completed"] * 2
        assert events[1]["body"]["task_preview"] == "first turn"
        assert events[1]["body"]["final_result_excerpt"].startswith("RESUMED")
        # delivery id is per turn, so turn 2 is a NEW run, not a duplicate
        ids = [e["headers"]["X-Request-ID"] for e in events]
        assert ids == [f"{view['job_id']}-t1", f"{view['job_id']}-t2"]
        # wake state was reset for the new turn and then delivered
        final = wait_wake(view["job_id"], extra_env=WAKE_FAST)
        assert final["wake"]["delivered"] is True
        assert final["wake"]["attempts"] == 1
        assert final["status"] == "completed"
    finally:
        srv.shutdown()


def test_no_wake_on_cancelled(env, workdir):
    srv = start_receiver(secret=SECRET)
    try:
        enable_wake(env, receiver_url(srv))
        view = submit(env, workdir, task="BRIDGE_SLEEP 120 never ending")
        deadline = time.time() + 10
        while time.time() < deadline:
            v = json.loads(bridge("status", view["job_id"], "--json",
                                  check=0).stdout)
            if v["status"] == "running":
                break
            time.sleep(0.2)
        assert v["status"] == "running"
        out = json.loads(bridge("cancel", view["job_id"], "--json",
                                check=0).stdout)
        assert out["status"] == "cancelled"
        time.sleep(2.0)  # grace: a late wake must not appear
        assert srv.requests == []
        v = json.loads(bridge("status", view["job_id"], "--json",
                              check=0).stdout)
        assert v["status"] == "cancelled"
        assert v["wake"]["delivered"] is False
        assert v["wake"]["attempts"] == 0
    finally:
        srv.shutdown()


def test_wake_retries_while_receiver_is_down(env, workdir):
    """Gateway-down case: keep posting every interval until it answers 2xx."""
    port = free_port()
    enable_wake(env, f"http://127.0.0.1:{port}/webhooks/pi-bridge-complete")
    view = submit(env, workdir, task="wake through a restart window")
    time.sleep(1.5)  # at least one failed attempt against the closed port
    v = json.loads(bridge("status", view["job_id"], "--json",
                          check=0).stdout)
    assert v["status"] == "completed"        # job status never depends on wake
    assert v["wake"]["delivered"] is False
    assert v["wake"]["attempts"] >= 1
    assert "unreachable" in v["wake"]["last_error"]

    srv = start_receiver(port=port, secret=SECRET)  # "gateway came back"
    try:
        ev = wait_events(srv, 1, timeout=20)
        assert ev[0]["verified"] is True
        assert ev[0]["body"]["job_id"] == view["job_id"]
        v = wait_wake(view["job_id"], extra_env=WAKE_FAST)
        assert v["wake"]["delivered"] is True and v["wake"]["last_error"] is None
        assert v["wake"]["attempts"] > 1
        assert v["status"] == "completed"
    finally:
        srv.shutdown()


def test_wake_retries_on_non_2xx(env, workdir):
    srv = start_receiver(secret=SECRET, responses=[503, 503, 202])
    try:
        enable_wake(env, receiver_url(srv))
        view = submit(env, workdir, task="flaky receiver")
        ev = wait_events(srv, 3)
        assert all(e["verified"] for e in ev)
        final = wait_wake(view["job_id"], extra_env=WAKE_FAST)
        assert final["status"] == "completed"
        assert final["wake"]["delivered"] is True
        assert final["wake"]["attempts"] == 3
    finally:
        srv.shutdown()


def test_wake_disabled_means_zero_requests_and_old_behaviour(env, workdir):
    srv = start_receiver(secret=SECRET)
    try:
        view = submit(env, workdir, task="no wake.json at all")
        final = wait_terminal(view["job_id"], extra_env=WAKE_FAST)
        assert final["status"] == "completed"
        assert final["final_result"].startswith("FAKEOK")
        assert final["wake"] == {"enabled": False, "delivered": False,
                                 "attempts": 0, "last_error": None}
        time.sleep(1.0)
        assert srv.requests == []
        assert job_json(env["home"], view["job_id"])["wake"]["enabled"] is False
    finally:
        srv.shutdown()


def test_wake_enabled_false_is_respected(env, workdir):
    srv = start_receiver(secret=SECRET)
    try:
        (env["home"] / "wake.json").write_text(json.dumps(
            {"enabled": False, "url": receiver_url(srv), "secret": SECRET}))
        view = submit(env, workdir, task="explicitly disabled")
        final = wait_terminal(view["job_id"], extra_env=WAKE_FAST)
        assert final["status"] == "completed"
        time.sleep(1.0)
        assert srv.requests == []
        assert final["wake"]["enabled"] is False
    finally:
        srv.shutdown()


def test_wake_exhaustion_does_not_change_job(env, workdir):
    port = free_port()
    enable_wake(env, f"http://127.0.0.1:{port}/webhooks/pi-bridge-complete")
    env2 = dict(WAKE_FAST, PI_BRIDGE_WAKE_MAX_ATTEMPTS="3")
    view = submit(env, workdir, task="nobody is listening", extra_env=env2)
    deadline = time.time() + 20
    while time.time() < deadline:
        v = json.loads(bridge("status", view["job_id"], "--json",
                              check=0, extra_env=env2).stdout)
        if v["wake"]["attempts"] >= 3:
            break
        time.sleep(0.3)
    # the job itself is untouched by the failed notification
    assert v["status"] == "completed"
    assert v["final_result"].startswith("FAKEOK")
    assert v["wake"]["enabled"] is True
    assert v["wake"]["delivered"] is False
    assert v["wake"]["attempts"] == 3
    assert "unreachable" in v["wake"]["last_error"]
    # and a normal feedback still works on such a job
    bridge("feedback", view["job_id"], "--feedback", "still fine", "--json",
           check=0, extra_env=env2)
    assert wait_terminal(view["job_id"], extra_env=env2)["status"] == "completed"


def test_wake_permanent_error_gives_up_early(env, workdir):
    """Wrong secret -> the gateway answers 401 forever; do not pin the runner
    for 45 minutes on a permanent client error."""
    srv = start_receiver(secret=SECRET)  # client uses a DIFFERENT secret
    try:
        enable_wake(env, receiver_url(srv), secret="totally-wrong-secret")
        view = submit(env, workdir, task="badly configured")
        final = wait_terminal(view["job_id"], extra_env=WAKE_FAST)
        assert final["status"] == "completed"   # job unaffected
        deadline = time.time() + 15
        while time.time() < deadline:
            v = json.loads(bridge("status", view["job_id"], "--json",
                                  check=0, extra_env=WAKE_FAST).stdout)
            if v["wake"]["attempts"] >= 3 and "permanent" in (v["wake"]["last_error"] or ""):
                break
            time.sleep(0.3)
        assert v["wake"]["delivered"] is False
        assert v["wake"]["attempts"] == 3
        assert "HTTP 401" in v["wake"]["last_error"]
        assert "permanent" in v["wake"]["last_error"]
        # the receiver really did reject the signatures
        assert len(srv.rejected) >= 3
        assert all(r["verified"] is False for r in srv.rejected)
        assert srv.events == []
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------
# pi_list
# --------------------------------------------------------------------------

LIST_KEYS = {"job_id", "status", "cwd", "created_at", "updated_at",
             "task_preview", "feedback_turns", "turns", "wake", "origin"}


def test_pi_list_rows_limit_and_no_transcript(env, workdir):
    jobs = []
    for i in range(3):
        v = submit(env, workdir, task=f"listable task {i} marker-XYZ")
        wait_terminal(v["job_id"], extra_env=WAKE_FAST)
        jobs.append(v["job_id"])
    # updated_at has one-second resolution: space the turns out so the
    # expected recency order is unambiguous.
    time.sleep(1.1)
    bridge("feedback", jobs[0], "--feedback", "more work", "--json",
           check=0, extra_env=WAKE_FAST)
    wait_terminal(jobs[0], extra_env=WAKE_FAST)
    time.sleep(1.1)
    bridge("feedback", jobs[1], "--feedback", "even newer", "--json",
           check=0, extra_env=WAKE_FAST)
    wait_terminal(jobs[1], extra_env=WAKE_FAST)

    rows = json.loads(bridge("list", "--json", check=0).stdout)
    assert {r["job_id"] for r in rows} == set(jobs)
    for r in rows:
        assert set(r) == LIST_KEYS
        assert set(r["wake"]) == {"delivered", "enabled"}
        assert r["task_preview"].startswith("listable task")
    # newest first by updated_at: the job that got the LAST feedback is first
    assert [r["job_id"] for r in rows] == [jobs[1], jobs[0], jobs[2]]
    assert rows[0]["feedback_turns"] == 1 and rows[0]["turns"] == 2
    assert rows[1]["feedback_turns"] == 1 and rows[1]["turns"] == 2
    assert rows[2]["feedback_turns"] == 0 and rows[2]["turns"] == 1
    updated = [r["updated_at"] for r in rows]
    assert updated == sorted(updated, reverse=True)
    assert updated[0] > updated[1] >= updated[2]
    # nothing but a preview: no final result, no transcript, no stderr
    out = bridge("list", "--json", check=0).stdout
    assert "FAKEOK" not in out                 # final results stay out
    for forbidden in ("final_result", "transcript", "stderr", "pi_session_file",
                      "log_dir", "runner_pid"):
        assert forbidden not in out, forbidden

    limited = json.loads(bridge("list", "--json", "--limit", "1",
                                check=0).stdout)
    assert len(limited) == 1
    assert limited[0]["job_id"] == jobs[1]
    assert json.loads(bridge("list", "--json", "--limit", "0",
                             check=0).stdout) == []

    human = bridge("list", check=0).stdout
    assert len(human.strip().splitlines()) == 3
    assert jobs[0] in human and "wake:" in human
    assert "FAKEOK" not in human


def test_pi_list_default_limit_applies(env, workdir):
    """--limit defaults to 20 so a long job history cannot flood the model."""
    ids = []
    for i in range(23):
        v = submit(env, workdir, task=f"flood {i}")
        ids.append(v["job_id"])
        # complete immediately: one cheap submit+wait per job
        wait_terminal(v["job_id"], extra_env=WAKE_FAST, timeout=30)
    rows = json.loads(bridge("list", "--json", check=0).stdout)
    assert len(rows) == 20
    assert len(json.loads(bridge("list", "--json", "--limit", "23",
                                 check=0).stdout)) == 23


def test_pi_list_recovers_a_running_job(env, workdir):
    """The recovery use case: after losing the job_id, pi_list finds it."""
    v = submit(env, workdir, task="BRIDGE_SLEEP 30 long running job")
    try:
        deadline = time.time() + 10
        rows = []
        while time.time() < deadline:
            rows = json.loads(bridge("list", "--json", check=0).stdout)
            if any(r["status"] == "running" for r in rows):
                break
            time.sleep(0.3)
        row = next(r for r in rows if r["job_id"] == v["job_id"])
        assert row["status"] == "running"
        assert row["cwd"] == str(workdir.resolve())
    finally:
        bridge("cancel", v["job_id"], check=0)


def test_plugin_exposes_five_tools_and_pi_list(env, workdir, monkeypatch):
    import importlib.util

    monkeypatch.setenv("PI_BRIDGE_CLI", str(REPO / ".venv" / "bin" / "pi-bridge"))
    monkeypatch.setenv("PI_BRIDGE_LAUNCHER", "detach")
    spec = importlib.util.spec_from_file_location(
        "pi_worker_plugin_wake_test", REPO / "plugin" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    registered = {}

    class Ctx:
        def register_tool(self, name, toolset, schema, handler, **kw):
            registered[name] = (schema, handler)

    mod.register(Ctx())
    assert sorted(registered) == ["pi_cancel", "pi_delegate", "pi_feedback",
                                  "pi_list", "pi_status"]
    for name, (schema, _) in registered.items():
        assert schema["name"] == name
        assert schema["description"].strip()
        assert schema["parameters"]["type"] == "object"

    # duplicate-prevention guidance is actually in the tool descriptions
    delegate_desc = registered["pi_delegate"][0]["description"]
    assert "pi_list FIRST" in delegate_desc
    assert "do NOT blindly re-delegate" in delegate_desc
    list_desc = registered["pi_list"][0]["description"]
    assert "re-delegating" in list_desc and "pi_delegate" in list_desc

    manifest = (REPO / "plugin" / "plugin.yaml").read_text()
    for tool in registered:
        assert f"- {tool}" in manifest

    # handler works and returns the compact rows
    v = submit(env, workdir, task="listed via plugin")
    wait_terminal(v["job_id"], extra_env=WAKE_FAST)
    out = json.loads(registered["pi_list"][1]({"limit": 5}))
    assert isinstance(out, list) and out[0]["job_id"] == v["job_id"]
    assert set(out[0]) == LIST_KEYS
    assert "error" in json.loads(registered["pi_list"][1]({"limit": "abc"}))
    assert "error" in json.loads(registered["pi_list"][1]({"limit": -3}))
    assert "error" in json.loads(registered["pi_list"][1](None))


def test_wake_over_systemd_launcher(env, workdir):
    """Production launcher path: the notifier runs inside the transient unit,
    which starts with a clean environment (hence the --setenv forwarding)."""
    if not os.path.exists("/run/user/%d/systemd" % os.getuid()):
        pytest.skip("no user systemd instance")
    srv = start_receiver(secret=SECRET)
    try:
        enable_wake(env, receiver_url(srv))
        r = bridge("submit", "--cwd", str(workdir), "--pi-bin", str(FAKE_PI),
                   "--task", "systemd wake me", "--json", check=0,
                   launcher="systemd", extra_env=WAKE_FAST)
        view = json.loads(r.stdout)
        assert view["launcher"] == "systemd" and view["unit"]
        final = wait_terminal(view["job_id"], extra_env=WAKE_FAST, timeout=40)
        assert final["status"] == "completed"
        v = wait_wake(view["job_id"], extra_env=WAKE_FAST)
        assert v["wake"]["delivered"] is True and v["wake"]["attempts"] >= 1
        ev = wait_events(srv, 1)
        assert ev[0]["verified"] is True
        assert ev[0]["body"]["job_id"] == view["job_id"]
        assert ev[0]["headers"]["X-Request-ID"] == f"{view['job_id']}-t1"
        # the unit exits by itself once the wake was accepted
        deadline = time.time() + 20
        while time.time() < deadline:
            if not json.loads(bridge("status", view["job_id"], "--json",
                                     check=0).stdout)["runner_alive"]:
                break
            time.sleep(0.3)
        assert not json.loads(bridge("status", view["job_id"], "--json",
                                     check=0).stdout)["runner_alive"]
    finally:
        srv.shutdown()


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def test_sigterm_stops_the_retry_loop(env, workdir):
    """Stopping the runner must end the notifier immediately: otherwise the
    unit burns TimeoutStopSec, gets SIGKILLed, and can fire one more wake
    after the operator already took the webhook route away."""
    port = free_port()
    enable_wake(env, f"http://127.0.0.1:{port}/webhooks/pi-bridge-complete")
    e = dict(WAKE_FAST, PI_BRIDGE_WAKE_INTERVAL="10",
             PI_BRIDGE_WAKE_MAX_ATTEMPTS="200")
    view = submit(env, workdir, task="nobody is listening, stop me")
    deadline = time.time() + 25
    v = None
    while time.time() < deadline:
        v = json.loads(bridge("status", view["job_id"], "--json", check=0,
                              extra_env=e).stdout)
        if (v["status"] == "completed" and v["wake"]["attempts"] >= 1
                and not v["wake"]["delivered"] and v["runner_alive"]):
            break
        time.sleep(0.3)
    assert v["runner_alive"] and v["runner_pid"], v
    pid, attempts_at_stop = v["runner_pid"], v["wake"]["attempts"]

    os.kill(pid, signal.SIGTERM)
    stop_deadline = time.time() + 10
    while _pid_alive(pid) and time.time() < stop_deadline:
        time.sleep(0.1)
    assert not _pid_alive(pid), f"runner {pid} survived SIGTERM"

    v2 = json.loads(bridge("status", view["job_id"], "--json",
                           check=0).stdout)
    assert v2["status"] == "completed"          # job unaffected
    assert v2["final_result"].startswith("FAKEOK")
    assert v2["wake"]["delivered"] is False
    assert "stopped by signal" in v2["wake"]["last_error"]
    # no further attempts were made after the signal
    assert v2["wake"]["attempts"] == attempts_at_stop
