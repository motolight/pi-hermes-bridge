"""Tests for the optional, read-only PI WEB observability layer.

A fake PI WEB (tests/fake_piweb.py) stands in for the real service; the
bridge must behave exactly as before in every case, whether PI WEB answers
slowly, never answers, refuses the path or knows nothing about the job.
"""
import json
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import FAKE_PI, REPO, bridge, job_json, wait_terminal
from fake_piweb import FakePiWeb, session_row

from pi_bridge import piweb, runner

DEAD_URL = "http://127.0.0.1:1"


def _cfg(tmp_path, roots, host="127.0.0.1", port=1):
    cfg = {"host": host, "port": port,
           "pathAccess": {"allowedPaths": [str(r) for r in roots]}}
    p = Path(tmp_path) / "pi-web-config-for-tests.json"
    p.write_text(json.dumps(cfg))
    return str(p)


def _web_env(web, tmp_path):
    return {"PI_WEB_URL": web.base, "PI_WEB_CONFIG": web.config(tmp_path)}


def _submit(cwd, extra_env, task="observe me"):
    r = bridge("submit", "--cwd", str(cwd), "--pi-bin", str(FAKE_PI),
               "--task", task, "--json", check=0, extra_env=extra_env)
    return json.loads(r.stdout)


def _status(job_id, extra_env):
    return json.loads(bridge("status", job_id, "--json", check=0,
                             extra_env=extra_env).stdout)


def _settled_status(job_id, extra_env, timeout=20):
    """Status once the runner process is really gone.

    `runner_alive` is the only time-dependent field of a terminal job: the
    runner exits a moment after writing the terminal status, so two views
    taken across that moment cannot be compared byte-for-byte.  Waiting for
    the process to disappear keeps the strict comparison strict.
    """
    deadline = time.time() + timeout
    v = None
    while time.time() < deadline:
        v = _status(job_id, extra_env)
        if v["status"] not in ("queued", "running") and not v["runner_alive"]:
            return v
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} never settled: {v}")


STATUS_KEYS_V1 = {
    "job_id", "status", "created_at", "updated_at", "cwd", "pi_session_id",
    "pi_session_file", "task_preview", "feedback_turns", "turns",
    "runner_alive", "stdout_capped", "launcher", "unit", "runner_pid",
    "error", "log_dir", "final_result_chars", "final_result_truncated",
    "final_result",
}
# V1.2 adds exactly one more key to the status view (wake delivery state),
# V1.3 adds the recorded delivery origin; PI WEB observability must still
# add nothing of its own.
ADDED_V1_2 = {"wake"}
ADDED_V1_3 = {"origin"}


def test_piweb_unavailable_leaves_job_behaviour_identical(env, workdir):
    """Connection-refused PI WEB: same job, same status view, only pi_web says no."""
    off_env = {"PI_WEB_URL": DEAD_URL,
               "PI_WEB_CONFIG": _cfg(env["tmp"], [env["tmp"]]),
               "PI_BRIDGE_NO_PIWEB": "1"}
    live_env = {"PI_WEB_URL": DEAD_URL,
                "PI_WEB_CONFIG": _cfg(env["tmp"], [env["tmp"]])}

    v_off = _submit(workdir, off_env, task="no pi web here")
    assert v_off["pi_web"] == {"available": False, "reason": "disabled-by-env"}
    v_live = _submit(workdir, live_env, task="no pi web here")
    assert v_live["status"] in ("queued", "running")
    # identical shape: pi_web + wake are the only additions over the V1 view
    assert set(v_live) == set(v_off) == (
        STATUS_KEYS_V1 | {"pi_web"} | ADDED_V1_2 | ADDED_V1_3)

    f_live = wait_terminal(v_live["job_id"], extra_env=live_env)
    assert f_live["status"] == "completed"
    assert f_live["final_result"].startswith("FAKEOK")
    assert set(f_live) == (
        STATUS_KEYS_V1 | {"pi_web"} | ADDED_V1_2 | ADDED_V1_3)

    # the very same job read with observability switched off must produce a
    # byte-identical view apart from pi_web -- compared only after the job
    # has fully settled (see _settled_status)
    f_live = _settled_status(v_live["job_id"], live_env)
    f_off = _status(v_live["job_id"], off_env)
    a = {k: v for k, v in f_off.items() if k != "pi_web"}
    b = {k: v for k, v in f_live.items() if k != "pi_web"}
    assert a == b

    pw = f_live["pi_web"]
    assert pw["available"] is False
    assert str(pw["reason"]).startswith("unreachable")
    assert pw.get("pi_web_url") is None


def test_project_is_registered_exactly_once(env, workdir):
    with FakePiWeb([env["tmp"]]) as web:
        e = _web_env(web, env["tmp"])
        v1 = _submit(workdir, e)
        pw = v1["pi_web"]
        assert pw["available"] is True
        assert pw["project_id"] and pw["workspace_id"]
        assert pw["project_name"] == workdir.name
        assert web.count("POST", "/api/projects") == 1

        # further observations and further jobs on the same cwd reuse it
        bridge("web-info", v1["job_id"], check=0, extra_env=e)
        _status(v1["job_id"], e)
        v2 = _submit(workdir, e)
        assert v2["pi_web"]["project_id"] == pw["project_id"]
        assert web.count("POST", "/api/projects") == 1
        assert len(web.snapshot_projects()) == 1


def test_session_visible_and_deep_link_tracks_message_count(env, workdir):
    with FakePiWeb([env["tmp"]]) as web:
        e = _web_env(web, env["tmp"])
        v = _submit(workdir, e)
        sid = v["pi_session_id"]
        # nothing known yet -> visible false, but the view is still available
        assert _status(v["job_id"], e)["pi_web"]["session_visible"] is False

        web.set_sessions(workdir, [session_row(sid, message_count=7,
                                               modified="2026-01-02T00:00:00.000Z")])
        pw = _status(v["job_id"], e)["pi_web"]
        assert pw["available"] is True
        assert pw["session_visible"] is True
        assert pw["message_count"] == 7
        assert pw["session_modified"] == "2026-01-02T00:00:00.000Z"
        url = pw["pi_web_url"]
        assert url.startswith(web.base + "/?")
        assert "machine=local" in url
        for ident in (pw["project_id"], pw["workspace_id"], sid):
            assert ident in url

        # the store is re-read on every call: no caching of messageCount
        web.set_sessions(workdir, [session_row(sid, message_count=12)])
        assert _status(v["job_id"], e)["pi_web"]["message_count"] == 12

        # web-info CLI returns the very same dict
        cli = json.loads(bridge("web-info", v["job_id"], check=0,
                                extra_env=e).stdout)
        assert cli == _status(v["job_id"], e)["pi_web"]


def test_hung_pi_web_neither_delays_nor_breaks_status(env, workdir):
    with FakePiWeb([env["tmp"]], latency=30) as web:
        e = _web_env(web, env["tmp"])
        v = _submit(workdir, e)
        assert v["pi_web"]["available"] is False

        t0 = time.time()
        st = _status(v["job_id"], e)
        elapsed = time.time() - t0
        assert elapsed < 6.0, f"status took {elapsed:.1f}s behind a hung PI WEB"
        assert st["job_id"] == v["job_id"]
        assert st["status"] in ("queued", "running", "completed")
        assert st["pi_web"]["available"] is False

        # and the job itself is completely unaffected by the hung observer
        final = wait_terminal(v["job_id"], extra_env=e)
        assert final["status"] == "completed"
        assert final["final_result"].startswith("FAKEOK")


def test_cwd_outside_allowed_root_never_registers_a_project(env, tmp_path):
    inside = env["tmp"] / "inside-root"
    inside.mkdir()
    outside = env["tmp"] / "outside-root"
    outside.mkdir()
    with FakePiWeb([inside]) as web:
        e = _web_env(web, env["tmp"])
        v = _submit(outside, e)
        assert v["pi_web"]["available"] is False
        assert v["pi_web"]["reason"] == "cwd-outside-allowed-paths"
        assert web.requests == []          # not even a status probe
        # the job still runs normally
        assert wait_terminal(v["job_id"], extra_env=e)["status"] == "completed"


def test_session_dir_is_not_passed_to_new_jobs(tmp_path, monkeypatch):
    """Standard-store lookup is primary, nested subagent files included, and
    the pi env-var session-dir override is never used."""
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))

    job_sessions = tmp_path / "jobs" / "pb-x" / "sessions"
    legacy = job_sessions / "--cwd--" / "2026-01-01T00-00-00-000Z_oldid.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}\n")
    assert runner.find_session_file(job_sessions, "oldid") == str(legacy)

    standard = fake_home / ".pi" / "agent" / "sessions" / "--cwd--"
    standard.mkdir(parents=True)
    main = standard / "2026-01-02T00-00-00-000Z_newid.jsonl"
    main.write_text("{}\n")
    assert runner.find_session_file(job_sessions, "newid") == str(main)
    # subagent sessions live one level deeper in the same store
    sub = standard / "subagents"
    sub.mkdir()
    subf = sub / "2026-01-03T00-00-00-000Z_subid.jsonl"
    subf.write_text("{}\n")
    assert runner.find_session_file(job_sessions, "subid") == str(subf)
    # the standard store wins over a legacy copy of the same session
    dup = job_sessions / "--cwd--" / "2026-01-04T00-00-00-000Z_newid.jsonl"
    dup.write_text("{}\n")
    assert runner.find_session_file(job_sessions, "newid") == str(main)

    argv_src = (REPO / "pi_bridge" / "runner.py").read_text()
    assert "PI_CODING_AGENT_SESSION_DIR" not in argv_src


def test_piweb_helpers_never_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_WEB_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.setenv("PI_WEB_URL", "http://127.0.0.1:1")
    assert piweb.availability()["available"] is False
    assert piweb.observe(str(tmp_path), "sid")["available"] is False
    assert piweb.observe_job({})["available"] is False
    assert piweb.ensure_project("/definitely/not/here") is None
    assert piweb.workspace_for(None) is None
    assert piweb.deep_link("p", None, "s") is None
    assert piweb.deep_link("p", "w", "s").endswith(
        "/?machine=local&project=p&workspace=w&session=s")
    # budget exhaustion degrades instead of hanging
    b = piweb._Budget(0.2)
    b.deadline = time.monotonic() - 1
    assert b.timeout() is None
    assert piweb.session_visible(str(tmp_path), "sid", budget=b)["visible"] is False


def test_plugin_passes_pi_web_through_bounded(env, workdir, monkeypatch):
    """pi_status stays valid, bounded JSON and carries the pi_web field."""
    import importlib.util

    monkeypatch.setenv("PI_BRIDGE_CLI", str(REPO / ".venv" / "bin" / "pi-bridge"))
    monkeypatch.setenv("PI_BRIDGE_PI_BIN", str(FAKE_PI))
    monkeypatch.setenv("PI_BRIDGE_LAUNCHER", "detach")
    monkeypatch.setenv("PI_BRIDGE_NO_PIWEB", "1")
    spec = importlib.util.spec_from_file_location(
        "pi_worker_plugin_piweb_test", REPO / "plugin" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    out = json.loads(mod.pi_delegate({"task": "BRIDGE_BIG", "cwd": str(workdir)}))
    job_id = out["job_id"]
    deadline = time.time() + 25
    while time.time() < deadline:
        raw = mod.pi_status({"job_id": job_id})
        v = json.loads(raw)
        if v.get("status") not in ("queued", "running"):
            break
        time.sleep(0.2)
    assert v["status"] == "completed"
    assert "pi_web" in v
    assert v["pi_web"] == {"available": False, "reason": "disabled-by-env"}
    # oversized final result is still capped, so the tool output stays bounded
    assert v["final_result_truncated"] is True
    assert len(v["final_result"]) <= 4000
    assert len(raw) < 20000


def test_legacy_v1_job_keeps_its_session_dir(env, workdir):
    """A job whose session lives in jobs/<id>/sessions (bridge <= V1) must NOT
    be silently re-created in the standard store, or it would lose all
    context while looking like a continuation."""
    v = _submit(workdir, {"PI_BRIDGE_NO_PIWEB": "1"})
    first = wait_terminal(v["job_id"])
    assert first["final_result"].startswith("FAKEOK")

    # emulate the V1 layout: session file inside the job dir
    src = Path(job_json(env["home"], v["job_id"])["pi_session_file"])
    norm = "--" + re.sub(r"[^a-zA-Z0-9]", "-", str(workdir)) + "--"
    legacy_dir = env["home"] / "jobs" / v["job_id"] / "sessions" / norm
    legacy_dir.mkdir(parents=True)
    dest = legacy_dir / src.name
    shutil.move(str(src), str(dest))
    assert not list((env["pi_home"] / ".pi" / "agent" / "sessions").rglob("*.jsonl"))

    bridge("feedback", v["job_id"], "--feedback", "second turn", "--json",
           check=0, extra_env={"PI_BRIDGE_NO_PIWEB": "1"})
    second = wait_terminal(v["job_id"])
    # RESUMED, not FAKEOK: pi was pointed at the legacy dir and saw turn 1
    assert second["final_result"].startswith("RESUMED"), second["final_result"]
    assert len(dest.read_text().strip().splitlines()) == 2
    assert not list((env["pi_home"] / ".pi" / "agent" / "sessions").rglob("*.jsonl"))
    assert job_json(env["home"], v["job_id"])["pi_session_file"] == str(dest)
    log = (env["home"] / "jobs" / v["job_id"] / "runner.log").read_text()
    assert "legacy --session-dir" in log


def test_time_budget_is_hard_bounded(tmp_path, monkeypatch):
    import time as _t

    from pi_bridge.piweb import (MAX_BUDGET, MAX_BODY, _Budget, _read_capped,
                                 _request)

    monkeypatch.setenv("PI_WEB_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("PI_WEB_CONFIG", str(tmp_path / "missing-config.json"))

    # an oversized budget is clamped: observability can never outlive a tool call
    b = _Budget(10_000)
    assert b.left() <= MAX_BUDGET + 0.001

    # expired budget -> refused, never urlopen(timeout=None)
    b.deadline = _t.monotonic() - 1
    assert _request("GET", "/api/projects", budget=b) == (
        None, None, "time-budget-exhausted")

    # a body that trickles forever is cut off at the deadline
    class Trickling:
        def __init__(self):
            self.n = 0

        def read(self, n):
            self.n += 1
            time.sleep(0.05)
            return b"x" * n

    class Trickling2(Trickling):
        def read(self, n):
            return b"x" * n

    assert _read_capped(Trickling(), _t.monotonic() + 0.15) is None

    class Ending(Trickling):
        def read(self, n):
            self.n += 1
            return b"x" * n if self.n <= 2 else b""

    got = _read_capped(Ending(), _t.monotonic() + 5)
    assert got is not None and len(got) == 2 * 64 * 1024

    # and a body that never ends is capped at MAX_BODY
    assert len(_read_capped(Trickling2(), _t.monotonic() + 30)) == MAX_BODY + 1


def test_refused_or_ghost_registration_is_not_re_polled(env, workdir):
    """Every `pi-bridge` poll is a fresh process: the memo must survive that,
    or a broken PI WEB would get one POST /api/projects per status poll."""
    for mode in ("refuse", "ghost"):
        # the memo is keyed by cwd and lives in the bridge home: start clean
        (env["home"] / piweb.REFUSED_REGISTRATIONS_FILE).unlink(missing_ok=True)
        with FakePiWeb([env["tmp"]], register_mode=mode) as web:
            e = _web_env(web, env["tmp"])
            v = _submit(workdir, e)
            assert v["pi_web"]["available"] is False
            assert v["pi_web"]["reason"] == "project-not-registered"
            for _ in range(3):
                assert _status(v["job_id"], e)["pi_web"]["reason"] == (
                    "project-not-registered")
                assert json.loads(bridge("web-info", v["job_id"], check=0,
                                         extra_env=e).stdout)["available"] is False
            assert web.count("POST", "/api/projects") == 1, mode
            assert web.count("GET", "/api/projects") > 1


def test_registration_memo_details(tmp_path, monkeypatch):
    """Only real refusals are memoized; transient failures keep retrying."""
    from pi_bridge import state

    home = tmp_path / "bridge-home"
    home.mkdir()
    monkeypatch.setenv("PI_BRIDGE_HOME", str(home))
    monkeypatch.setenv("PI_WEB_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("PI_WEB_CONFIG", str(tmp_path / "missing-config.json"))
    target = tmp_path / "proj"
    target.mkdir()
    real = str(target.resolve())
    cache_file = home / piweb.REFUSED_REGISTRATIONS_FILE

    def install(post_result, listed):
        calls = []

        def fake_request(method, path, params=None, body=None, budget=None):
            calls.append((method, path))
            if method == "POST":
                return post_result
            return (200, listed, None)

        monkeypatch.setattr(piweb, "_request", fake_request)
        return calls

    # transient transport failure -> no memo, retried next call
    calls = install((None, None, "unreachable: ECONNREFUSED"), [])
    assert piweb.register_project(str(target)) is None
    assert not cache_file.exists()
    assert piweb.register_project(str(target)) is None
    assert [c for c in calls if c[0] == "POST"] == [
        ("POST", "/api/projects"), ("POST", "/api/projects")]

    # HTTP refusal -> memoized, second call does not POST again
    calls = install((403, {"error": "not allowed"}, None), [])
    assert piweb.register_project(str(target)) is None
    assert json.loads(cache_file.read_text())[real] > time.time()
    before = [c for c in calls if c[0] == "POST"]
    assert piweb.register_project(str(target)) is None
    assert [c for c in calls if c[0] == "POST"] == before

    # accepted but never listed -> memoized too
    cache_file.unlink()
    calls = install((201, {"id": "ghost"}, None), [])
    assert piweb.register_project(str(target)) is None
    assert json.loads(cache_file.read_text())[real] > time.time()

    # accepted and listed -> project returned, nothing memoized
    cache_file.unlink()
    listed = [{"id": "p1", "name": "proj", "path": real}]
    install((201, {"id": "p1"}, None), listed)
    assert piweb.register_project(str(target))["id"] == "p1"
    assert not cache_file.exists()

    # an expired memo is ignored
    cache_file.write_text(json.dumps({real: time.time() - 1}))
    calls = install((201, {"id": "p1"}, None), listed)
    assert piweb.register_project(str(target))["id"] == "p1"
    assert ("POST", "/api/projects") in calls


def test_session_file_lookup_is_exact(tmp_path, monkeypatch):
    """`*_<id>.jsonl` must not suffix-match a different session whose id ends
    with ours (session ids may contain underscores)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store = tmp_path / "home" / ".pi" / "agent" / "sessions" / "--cwd--"
    store.mkdir(parents=True)
    ours = store / "2026-01-01T00-00-00-000Z_abc.jsonl"
    other = store / "2026-01-02T00-00-00-000Z_prefix_abc.jsonl"
    ours.write_text("{}\n")
    other.write_text("{}\n")
    assert runner.find_session_file_in(store.parent, "abc") == str(ours)
    assert runner.find_session_file_in(store.parent, "prefix_abc") == str(other)


def test_strings_from_pi_web_are_clamped(env, workdir):
    fat = {"id": "P" * 5000, "name": "N" * 5000,
           "path": str(workdir.resolve()), "createdAt": "2026-01-01T00:00:00.000Z"}
    with FakePiWeb([env["tmp"]], projects=[fat]) as web:
        e = _web_env(web, env["tmp"])
        v = _submit(workdir, e)
        pw = v["pi_web"]
        assert pw["available"] is True
        assert len(pw["project_name"]) <= piweb.NAME_KEEP
        assert len(pw["project_id"]) <= piweb.ID_KEEP
        assert len(pw["workspace_id"]) <= piweb.ID_KEEP
        assert len(pw["pi_web_url"]) <= piweb.URL_KEEP
        status = json.dumps(_status(v["job_id"], e), ensure_ascii=False)
        # the 5000-char strings never reach the tool output
        assert "N" * 200 not in status and "P" * 200 not in status
        assert len(status) < 20000
