"""Stall watchdog (v0.5.0) — detection, diagnosis, tier-1 recovery, events.

The regression case pinned here is the production incident
pb-20261021T191120Z-532dc0 (see docs/STALL_WATCHDOG.md): a bridge job's Pi
ran a foreground descendant command that looped forever; runner alive, Pi
alive, JSONL stale, descendant CPU ~0, backend idle.  Manual tier-1 (SIGTERM
to the verified descendant subtree only) restored the session in seconds.

Every test uses the sandboxed `env` fixture (private PI_BRIDGE_HOME + HOME)
and REAL processes under /proc — the process-tree verification and CPU
sampling are exercised for real, never mocked.  Nothing here touches the
production bridge home or any real Pi process.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pi_bridge import bridge, cli, state, watchdog

REPO = Path(__file__).resolve().parent.parent
FAKE_HERMES = REPO / "tests" / "fake_hermes.py"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# The fake "Pi": a real process that owns a real child tool command, exactly
# like the incident.  Modes:
#   hang  - one hanging child (sleep 600); when the child dies (SIGTERM'd by
#           the watchdog) the fake Pi "resumes": appends to the session JSONL
#           and exits 0 (same-session continuation, the recovered case).
#   loop  - respawn a hanging child forever, never resume (recovery does not
#           take -> escalation path; the Pi process itself stays alive).
#   cpu   - a CPU-burning child for 120 s (a legitimate busy build/test).
#   quiet - a child that periodically writes output (a legitimate long test
#           suite with periodic progress; the test refreshes the JSONL to
#           simulate visible progress).
#   none  - no child at all (tier-2 shape: Pi itself silent, nothing to kill).
FAKE_PI = '''\
import subprocess, sys, time, pathlib, os
mode, jd, jsonl = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
def child(cmd):
    p = subprocess.Popen(cmd)
    pathlib.Path(jd / "child.pid").write_text(str(p.pid))
    return p
if mode == "hang":
    p = child(["bash", "-c", "sleep 600"])
    p.wait()
    with open(jsonl, "a") as f:
        f.write('{"resumed": true}\\n')
    pathlib.Path(jd / "resumed").write_text("ok")
elif mode == "loop":
    while True:
        p = child(["bash", "-c", "sleep 600"])
        p.wait()
        time.sleep(0.3)
elif mode == "cpu":
    p = child(["bash", "-c", "end=$((SECONDS+120)); while [ $SECONDS -lt $end ]; do :; done"])
    p.wait()
elif mode == "quiet":
    p = child(["bash", "-c", "for i in $(seq 1 600); do echo tick; sleep 1; done"])
    p.wait()
elif mode == "none":
    time.sleep(600)
'''


class FakePi:
    """A live fake-Pi process with a JSONL session file and a job.json."""

    def __init__(self, env, mode, *, turn_age_s=400.0, jsonl_age_s=400.0,
                 origin=None):
        self.mode = mode
        self.home = env["home"]
        self.job_id = state.gen_job_id()
        self.session_id = "wd-" + self.job_id[3:] + "-s"
        self.jd = state.job_dir(self.job_id)
        (self.jd / "turns").mkdir(parents=True)
        (self.jd / "sessions").mkdir()
        self.jsonl = self.jd / "sessions" / f"2026-10-21T000000Z_{self.session_id}.jsonl"
        self.jsonl.write_text('{"seed": 1}\n')
        script = self.jd / "fake_pi.py"
        script.write_text(FAKE_PI)
        now = time.time()
        self.proc = subprocess.Popen(
            [sys.executable, str(script), mode, str(self.jd), str(self.jsonl)])
        self._add_cleanup(self.proc)
        # wait until the child exists (mode != none) so /proc evidence is real
        if mode != "none":
            deadline = time.time() + 5
            while time.time() < deadline:
                tree = watchdog.process_tree()
                if watchdog.direct_tool_roots(self.proc.pid, tree):
                    break
                time.sleep(0.05)
            # let the tool command age past the fixture's descendant_min_age_s
            time.sleep(1.2)
        past = iso(now - turn_age_s)
        job = {
            "job_id": self.job_id, "created_at": past, "updated_at": past,
            "cwd": str(self.jd), "status": "running",
            "pi_session_id": self.session_id, "pi_session_file": str(self.jsonl),
            "pi_bin": sys.executable, "task": "watchdog fixture task",
            "feedbacks": [],
            "turns": [{"n": 1, "kind": "task", "started_at": past,
                       "finished_at": None, "exit_code": None,
                       "pi_pid": self.proc.pid,
                       "pi_start": state.pid_start_ticks(self.proc.pid)}],
            "last_run": {"launcher": "detach", "unit": None,
                         "pid": os.getpid(), "started_at": past},
            "cancel_requested": False, "env_snapshot": {},
            "wake": {"enabled": False, "delivered": False, "attempts": 0,
                     "last_error": None},
            "final_result": None, "final_result_chars": 0,
            "final_result_truncated": False, "error": None,
        }
        if origin:
            job["origin"] = dict(origin)
        state.save_job(job)
        self.stale_jsonl(jsonl_age_s)
        # audit/last_action writes must not count: touch activity to the
        # intended stale point AFTER job.json was written
        self.stale_jsonl(jsonl_age_s)

    def _add_cleanup(self, proc):
        self._procs = getattr(self, "_procs", [])
        self._procs.append(proc)

    def stale_jsonl(self, age_s):
        ts = time.time() - age_s
        os.utime(self.jsonl, (ts, ts))
        # turn out/err do not exist; runner.log does not exist in the fixture

    def child_pid(self):
        try:
            return int((self.jd / "child.pid").read_text())
        except (OSError, ValueError):
            return None

    def alive(self, pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def pi_alive(self):
        return self.proc.poll() is None

    def child_alive(self):
        return self.alive(self.child_pid())

    def cleanup(self):
        for p in getattr(self, "_procs", []):
            if p.poll() is None:
                # kill the fake pi AND its (possibly orphaned) hanging child
                try:
                    tree = watchdog.process_tree()
                    for d in reversed(watchdog.descendants_of(p.pid, tree)):
                        try:
                            os.kill(d, 9)
                        except OSError:
                            pass
                except Exception:
                    pass
                p.kill()
            try:
                p.wait(timeout=2)
            except Exception:
                pass


@pytest.fixture()
def fixture_pi(env):
    made = []

    def build(mode, **kw):
        f = FakePi(env, mode, **kw)
        made.append(f)
        return f

    yield build
    for f in made:
        f.cleanup()


def cfg(**over) -> dict:
    c = {k: v for k, v in watchdog.DEFAULTS.items()}
    c["cpu_sample_interval_s"] = 0.15   # keep the suite fast
    c["stall_after_s"] = 30
    c["term_grace_s"] = 0.5
    c["confirm_grace_s"] = 0.8
    c["descendant_min_age_s"] = 1.0     # fixture children are seconds old
    c.update(over)
    return c


def tick(job_id, **over):
    job = state.load_job(job_id)
    return watchdog.tick_job(job, cfg(**over), time.time())


def wd(job_id) -> dict:
    return watchdog.normalize_state(state.load_job(job_id))


# ---------------------------------------------------------------------------
# (1) NO reaction before the threshold
# ---------------------------------------------------------------------------

def test_no_reaction_before_threshold(env, fixture_pi):
    f = fixture_pi("hang", jsonl_age_s=5)          # silent only 5 s
    res = tick(f.job_id, stall_after_s=30)
    assert res["action"] in ("healthy", "startup-grace")
    assert f.pi_alive() and f.child_alive()
    assert wd(f.job_id)["state"] == "healthy"
    assert not (f.jd / "watchdog.jsonl").exists()


# ---------------------------------------------------------------------------
# (2) confirmed stall -> tier-1 recovery -> resumed JSONL -> recovered
#     (the production regression case, synthetic)
# ---------------------------------------------------------------------------

def test_confirmed_stall_recovers_and_session_resumes(env, fixture_pi):
    f = fixture_pi("hang")                         # 400 s silence
    res = tick(f.job_id)
    assert res["action"] == "recovered-attempt"
    r = res["recover"]
    assert r["ok"] and r["terminated"], r
    # (3) Pi itself NOT killed; (4) only the descendant command terminated
    assert all(t["pid"] != f.proc.pid for t in r["terminated"])
    time.sleep(0.7)                                # term grace already 0.5 s
    assert not f.child_alive()
    # (3) Pi itself was NOT killed: the fake Pi exits by ITSELF, cleanly
    # (rc 0, resume file written).  A killed Pi would show rc -15/-9.
    try:
        rc = f.proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        rc = None                                  # still reasoning: fine
    assert rc in (None, 0), f"fake pi exited with {rc} (killed?)"
    # (5) the fake Pi resumes (JSONL advanced) and completes on its own
    deadline = time.time() + 5
    while time.time() < deadline and not (f.jd / "resumed").exists():
        time.sleep(0.05)
    assert (f.jd / "resumed").exists()
    # (6) the watchdog records recovered on its next tick
    res2 = tick(f.job_id)
    assert wd(f.job_id)["state"] == "recovered"
    w = wd(f.job_id)
    assert w["recovered_at"] and w["attempts"] == 1
    assert w["last_action"]["ok"] is True
    # the job status itself was NEVER touched by the watchdog
    assert state.load_job(f.job_id)["status"] == "running"
    # audit trail exists and names the terminated command
    audit = (f.jd / "watchdog.jsonl").read_text()
    assert "stall-confirmed" in audit and "recover-tier1" in audit \
        and "recovered" in audit


# ---------------------------------------------------------------------------
# negative cases: never auto-interrupted
# ---------------------------------------------------------------------------

def test_cpu_active_long_build_is_not_stalled(env, fixture_pi):
    f = fixture_pi("cpu")
    res = tick(f.job_id)
    assert res["action"] == "snoozed"
    assert "cpu-active" in res.get("reasons", [])
    # second pass: still snoozed (never acted on), nothing killed
    assert tick(f.job_id)["action"] == "snoozed"
    assert f.pi_alive() and f.child_alive()
    assert wd(f.job_id)["attempts"] == 0
    audit = (f.jd / "watchdog.jsonl").read_text() \
        if (f.jd / "watchdog.jsonl").exists() else ""
    assert "recover-tier1" not in audit


def test_periodic_output_long_test_is_not_stalled(env, fixture_pi):
    f = fixture_pi("quiet")
    # periodic output refreshes the activity signal (like JSONL appends as
    # Pi receives tool progress): the watchdog never even gets suspicious
    for _ in range(3):
        f.stale_jsonl(1)                            # output 1s ago
        res = tick(f.job_id)
        assert res["action"] in ("healthy", "startup-grace")
        time.sleep(0.05)
    assert f.pi_alive() and f.child_alive()
    assert wd(f.job_id)["state"] == "healthy"


def test_untracked_processes_outside_the_tree_are_never_touched(env, fixture_pi):
    f = fixture_pi("hang")
    # an unrelated sleep that is NOT a descendant of the job's Pi
    stranger = subprocess.Popen(["sleep", "300"])
    try:
        res = tick(f.job_id)
        assert res["action"] == "recovered-attempt"
        assert f.child_pid() not in (None, stranger.pid)
        assert all(t["pid"] != stranger.pid for t in res["recover"]["terminated"])
        os.kill(stranger.pid, 0)                    # still alive: untouched
    finally:
        stranger.kill()
        stranger.wait()


# ---------------------------------------------------------------------------
# bounded escalation: max 2 attempts, then needs_attention; Pi never killed
# ---------------------------------------------------------------------------

def test_max_two_attempts_then_needs_attention(env, fixture_pi):
    f = fixture_pi("loop")
    seen = []
    deadline = time.time() + 25
    while time.time() < deadline:
        res = tick(f.job_id, confirm_grace_s=0.2)
        seen.append(res["action"])
        if res["action"] == "needs-attention":
            break
        time.sleep(0.2)
    assert "recovered-attempt" in seen
    w = wd(f.job_id)
    assert w["state"] == "needs_attention"
    assert w["attempts"] == 2                       # never more than the cap
    assert w["needs_attention_at"]
    assert f.pi_alive()                             # Pi NEVER killed
    # further ticks change nothing (idempotent, no third attempt)
    tick(f.job_id)
    assert wd(f.job_id)["attempts"] == 2
    audit = (f.jd / "watchdog.jsonl").read_text()
    assert audit.count("recover-tier1") == 2
    assert "needs-attention" in audit


# ---------------------------------------------------------------------------
# tier-2 shape: Pi silent with nothing to interrupt -> report, never kill
# ---------------------------------------------------------------------------

def test_tier2_pi_silent_never_auto_recovered(env, fixture_pi):
    f = fixture_pi("none")
    r1 = tick(f.job_id)
    assert r1["action"] == "tier2-suspected"
    assert f.pi_alive()
    # a second identical strike escalates WITHOUT any recovery attempt
    job = state.load_job(f.job_id)
    w = watchdog.normalize_state(job)
    w["snoozed_until"] = None
    job["watchdog"] = w
    state.save_job(job)
    r2 = tick(f.job_id)
    assert r2["action"] == "needs-attention"
    assert wd(f.job_id)["attempts"] == 0            # ZERO tier-1 attempts
    assert f.pi_alive()                             # nothing was killed


# ---------------------------------------------------------------------------
# races + scope
# ---------------------------------------------------------------------------

def test_terminal_jobs_and_dead_runners_are_out_of_scope(env, fixture_pi,
                                                         monkeypatch):
    f = fixture_pi("hang")
    # normal completion pipeline owns the job: status completed -> skip
    with state.job_lock(f.job_id):
        j = state.load_job(f.job_id)
        j["status"] = "completed"
        j["turns"][-1]["finished_at"] = state.now_iso()
        state.save_job(j)
    summary = watchdog._tick()
    assert summary.get("scanned", 0) == 0
    assert f.child_alive()                          # nothing touched

    # dead runner pid -> skipped as well (reconcile owns it, not watchdog)
    j2 = state.load_job(f.job_id)
    with state.job_lock(f.job_id):
        j2 = state.load_job(f.job_id)
        j2["status"] = "running"
        j2["turns"][-1]["finished_at"] = None
        j2["last_run"] = {"launcher": "detach", "unit": None,
                          "pid": 999999, "started_at": iso(time.time() - 400)}
        state.save_job(j2)
    # pid 999999 may exist; force it to a definitely-dead one we own reaped
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    with state.job_lock(f.job_id):
        j2 = state.load_job(f.job_id)
        j2["last_run"]["pid"] = dead.pid
        state.save_job(j2)
    summary = watchdog._tick()
    assert summary.get("scanned", 0) == 0


def test_tick_lock_serialises_ticks(env, fixture_pi):
    watchdog.watchdog_dir().mkdir(parents=True, exist_ok=True)
    lock = open(watchdog.watchdog_dir() / "tick.lock", "a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        assert watchdog.tick() == {"skipped": "another-tick-running"}
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def test_recover_refuses_non_active_and_revalidates_pid(env, fixture_pi):
    f = fixture_pi("hang")
    with state.job_lock(f.job_id):
        j = state.load_job(f.job_id)
        j["status"] = "completed"
        state.save_job(j)
    assert watchdog.recover(f.job_id)["reason"] == "job-not-active"

    with state.job_lock(f.job_id):
        j = state.load_job(f.job_id)
        j["status"] = "running"
        state.save_job(j)
    # a live Pi with no children -> nothing to interrupt, no error, no kill
    f2 = fixture_pi("none")
    res = watchdog.recover(f2.job_id)
    assert res["ok"] is False and res["reason"] == "no-verified-descendants"
    assert f2.pi_alive()


def test_recover_survives_pid_identity_check(env, fixture_pi):
    """If the recorded Pi start-time no longer matches, recover refuses."""
    f = fixture_pi("hang")
    with state.job_lock(f.job_id):
        j = state.load_job(f.job_id)
        j["turns"][-1]["pi_start"] = 1              # wrong identity
        state.save_job(j)
    res = watchdog.recover(f.job_id)
    assert res["ok"] is False and "pi-not-verified" in res["reason"]
    assert f.pi_alive() and f.child_alive()         # nothing signalled


# ---------------------------------------------------------------------------
# stall events -> Hermes (separate event type, durable retry, HMAC V2)
# ---------------------------------------------------------------------------

def _wake_json(env, url, secret):
    p = state.bridge_home() / "wake.json"
    p.write_text(json.dumps({"enabled": True, "url": url, "secret": secret}))
    os.chmod(p, 0o600)


def test_stall_event_delivered_and_deduped(env, fixture_pi):
    sys.path.insert(0, str(REPO / "tests"))
    import fake_webhook
    secret = "s" * 40
    srv = fake_webhook.start_receiver(port=0, secret=secret)
    try:
        port = srv.server_address[1]
        _wake_json(env, f"http://127.0.0.1:{port}/webhooks/pi-bridge-complete",
                   secret)
        f = fixture_pi("hang")
        assert watchdog.stall_url() == \
            f"http://127.0.0.1:{port}/webhooks/{watchdog.STALL_ROUTE_NAME}"
        assert watchdog.queue_event(f.job_id, "stall")
        stats = watchdog.flush_events(cfg())
        assert stats["delivered"] == 1, stats
        ev = srv.events[0]
        assert ev["path"] == "/webhooks/" + watchdog.STALL_ROUTE_NAME
        assert ev["body"]["event_type"] == "pi_bridge_stall"
        assert ev["body"]["job_id"] == f.job_id
        assert "watchdog" in ev["body"]
        assert ev["headers"]["X-Request-ID"] == \
            f"{f.job_id}-t1-stall"                  # stable id -> dedup
        # spooled once: a second flush finds nothing (no duplicate wake)
        assert watchdog.flush_events(cfg())["delivered"] == 0
        assert len(srv.events) == 1
    finally:
        srv.shutdown()


def test_stall_event_retries_then_expires(env, fixture_pi):
    sys.path.insert(0, str(REPO / "tests"))
    import fake_webhook
    secret = "s" * 40
    srv = fake_webhook.start_receiver(port=0, secret=secret, responses=[500])
    try:
        port = srv.server_address[1]
        _wake_json(env, f"http://127.0.0.1:{port}/webhooks/pi-bridge-complete",
                   secret)
        f = fixture_pi("hang")
        watchdog.queue_event(f.job_id, "stall")
        stats = watchdog.flush_events(cfg())
        assert stats == {"delivered": 0, "retry": 1, "expired": 0}
        assert list(watchdog.pending_dir().glob("*.json"))
        # expire window spent -> moved to the audit events dir, not retried
        rec_path = next(iter(watchdog.pending_dir().glob("*.json")))
        rec = json.loads(rec_path.read_text())
        rec["created_at"] = iso(time.time() - 3600)
        rec_path.write_text(json.dumps(rec))
        stats = watchdog.flush_events(cfg())
        assert stats["expired"] == 1 and stats["retry"] == 0
        assert not list(watchdog.pending_dir().glob("*.json"))
        archived = watchdog.events_dir() / rec_path.name
        assert json.loads(archived.read_text())["status"] == "expired"
    finally:
        srv.shutdown()


def test_stall_url_none_when_wake_disabled(env, fixture_pi):
    assert watchdog.stall_url() is None
    assert watchdog.queue_event("pb-20200101T000000Z-abcdef", "stall") is None


# ---------------------------------------------------------------------------
# needs_attention user message (origin channel, argv-only hermes send)
# ---------------------------------------------------------------------------

def test_attention_user_message_uses_origin_send(env, fixture_pi,
                                                 monkeypatch):
    shim = env["tmp"] / "fake-hermes"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_HERMES} \"$@\"\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PI_BRIDGE_HERMES_BIN", str(shim))
    argv_out = env["tmp"] / "argv.jsonl"
    monkeypatch.setenv("FAKE_HERMES_ARGV_OUT", str(argv_out))
    monkeypatch.setenv("FAKE_HERMES_MODE", "ok")
    _wake_json(env, "http://127.0.0.1:1/webhooks/pi-bridge-complete",
               "s" * 40)
    f = fixture_pi("hang", origin={"platform": "telegram", "chat_id": "42"})
    res = watchdog.notify_user_attention(f.job_id)
    assert res["ok"] is True, res
    argv = json.loads(argv_out.read_text().splitlines()[-1])
    assert argv[:3] == ["send", "-t", "telegram:42"]
    assert "WATCHDOG" in argv[3] and f.job_id in argv[3]
    assert watchdog.normalize_state(
        state.load_job(f.job_id))["last_action"]["user_message"]["ok"]


# ---------------------------------------------------------------------------
# CLI + view integration
# ---------------------------------------------------------------------------

def test_cli_recover_and_watchdog_commands(env, fixture_pi, capsys):
    f = fixture_pi("hang")
    assert cli.main(["recover", f.job_id, "--json"]) == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["ok"] and out["trigger"] == "cli"
    assert not f.child_alive()
    try:
        rc = f.proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        rc = None
    assert rc in (None, 0)                          # never killed
    assert cli.main(["watchdog", "status", "--json"]) == 0
    sv = json.loads(capsys.readouterr().out.strip())
    row = next(j for j in sv["jobs"] if j["job_id"] == f.job_id)
    assert row["state"] in ("recovering", "recovered")
    assert cli.main(["watchdog", "tick"]) == 0
    capsys.readouterr()


def test_status_and_list_views_carry_watchdog(env, fixture_pi):
    f = fixture_pi("hang")
    v = bridge.status_view(state.load_job(f.job_id))
    assert v["watchdog"]["state"] == "healthy"      # untouched job: default
    # a tick that CONFIRMS and attempts tier-1 leaves the job in `recovering`
    res = tick(f.job_id)
    assert res["action"] == "recovered-attempt"
    v = bridge.status_view(state.load_job(f.job_id))
    assert v["watchdog"]["state"] == "recovering"
    assert v["watchdog"]["attempts"] == 1
    assert v["watchdog"]["last_action"]["trigger"] == "watchdog:confirmed-stall"
    rows = bridge.list_rows([state.load_job(f.job_id)])
    assert rows[0]["watchdog"]["state"] == "recovering"
    # old-shape jobs without a watchdog block still render a safe default
    legacy = state.load_job(f.job_id)
    legacy.pop("watchdog", None)
    assert bridge.status_view(legacy)["watchdog"]["state"] == "healthy"


def test_config_precedence(env, monkeypatch):
    home = state.bridge_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps(
        {"watchdog": {"stall_after_s": 300, "enabled": True}}))
    assert watchdog.load_config()["stall_after_s"] == 300
    monkeypatch.setenv("PI_BRIDGE_WATCHDOG_STALL_AFTER_S", "60")
    assert watchdog.load_config()["stall_after_s"] == 60
    monkeypatch.setenv("PI_BRIDGE_WATCHDOG_ENABLED", "0")
    assert watchdog.load_config()["enabled"] is False


# ---------------------------------------------------------------------------
# backend signal (vLLM /metrics): busy defers, unreachable never vetoes
# ---------------------------------------------------------------------------

def _busy_backend(url, timeout=3.0):
    return {"configured": True, "reachable": True, "running": 2,
            "waiting": 1, "idle": False}


def _down_backend(url, timeout=3.0):
    return {"configured": True, "reachable": False}


def test_backend_busy_defers_confirmation(env, fixture_pi, monkeypatch):
    monkeypatch.setattr(watchdog, "backend_activity", _busy_backend)
    f = fixture_pi("hang")
    r = tick(f.job_id)
    assert r["action"] == "confirm-deferred"        # one shared-backend busy
    assert f.child_alive()                          # not acted on yet
    # the next EVALUATION (a minute later in production; here the test
    # releases the one-minute recheck snooze) confirms twice -> act
    with state.job_lock(f.job_id):
        j = state.load_job(f.job_id)
        j["watchdog"]["snoozed_until"] = None
        state.save_job(j)
    r2 = tick(f.job_id)
    assert r2["action"] == "recovered-attempt"


def test_backend_down_still_confirms(env, fixture_pi, monkeypatch):
    monkeypatch.setattr(watchdog, "backend_activity", _down_backend)
    f = fixture_pi("hang")
    assert tick(f.job_id)["action"] == "recovered-attempt"


def test_backend_metrics_parser(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(SAMPLE_METRICS))
    r = watchdog.backend_activity("http://x/metrics")
    assert r["reachable"] and r["idle"] is False and r["running"] == 2
    monkeypatch.setattr(watchdog.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(SAMPLE_METRICS_IDLE))
    assert watchdog.backend_activity("http://x/metrics")["idle"] is True


class _Resp:
    def __init__(self, body):
        self.body = body.encode()

    def read(self, n=-1):
        return self.body[:n if n and n > 0 else None]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


SAMPLE_METRICS = """# HELP vllm:num_requests_running
vllm:num_requests_running{engine="0",model_name="m"} 2.0
vllm:num_requests_waiting{engine="0",model_name="m"} 1.0
vllm:num_requests_waiting_by_reason{engine="0",model_name="m",reason="capacity"} 1.0
"""
SAMPLE_METRICS_IDLE = """vllm:num_requests_running{engine="0",model_name="m"} 0.0
vllm:num_requests_waiting{engine="0",model_name="m"} 0.0
"""


# ---------------------------------------------------------------------------
# terminal-job observation cleanup (found in the v0.5.0 live smoke test:
# a job can complete BEFORE the next tick confirms the recovery)
# ---------------------------------------------------------------------------

def test_terminal_job_finalizes_dangling_recovery(env, fixture_pi):
    f = fixture_pi("hang")
    assert tick(f.job_id)["action"] == "recovered-attempt"
    assert wd(f.job_id)["state"] == "recovering"
    # normal completion pipeline wins the race: job terminal, unconfirmed
    with state.job_lock(f.job_id):
        j = state.load_job(f.job_id)
        j["status"] = "completed"
        j["turns"][-1]["finished_at"] = state.now_iso()
        state.save_job(j)
    watchdog._tick()                       # one tick finalizes observation
    w = wd(f.job_id)
    assert w["state"] == "recovered" and w["recovered_at"]
    assert state.load_job(f.job_id)["status"] == "completed"   # status untouched


def test_terminal_failed_job_keeps_attention(env, fixture_pi):
    f = fixture_pi("loop")
    tick(f.job_id)                          # attempt 1 -> recovering
    with state.job_lock(f.job_id):
        j = state.load_job(f.job_id)
        j["status"] = "failed"
        j["turns"][-1]["finished_at"] = state.now_iso()
        state.save_job(j)
    watchdog._tick()
    # a FAILED end is not "recovered": the attention flag stays visible
    assert wd(f.job_id)["state"] == "needs_attention"
