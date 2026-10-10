"""Writer tests: run writer.py against synthetic PI_BRIDGE_HOME copies.

Covers: running / stalled (stale mtimes) / interrupted (dead runner) /
done / failed views, terminal TTL pruning, non-webui origin exclusion, that no
task text or cwd ever reaches status.json, that filesystem paths in `error` are
redacted, that a terminal job's duration is fixed, that the writer leaves
PI_BRIDGE_HOME byte-for-byte unchanged, and that PI WEB links are opt-in and
restricted to private addresses.  Also covers the 1.1 UI fields: finished_at
from the last turn (falling back to updated_at) and the active / recent /
quiet / aged grouping that decides what the badge counts and what the card
lists.  PI_LAMP_PIWEB=0 keeps the view tests free of
any pi-web dependency.  Nothing here touches the real bridge state.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WRITER = HERE.parent / "writer.py"

import subprocess as _sp
_python3 = _sp.run(["which", "python3.12"], capture_output=True, text=True).stdout.strip() or sys.executable


def make_job(root: Path, job_id: str, *, status="running", origin=None,
             created="-300", updated="-30", turns=None, delivery=None,
             last_run=None, error=None, task="SECRET task text", cwd="/secret/cwd",
             final_result_chars=0):
    d = root / "jobs" / job_id
    d.mkdir(parents=True)
    job = {
        "job_id": job_id,
        "status": status,
        "created_at": _iso(time.time() + int(created)),
        "updated_at": _iso(time.time() + int(updated)),
        "cwd": cwd,
        "task": task,
        "pi_session_id": "11111111-1111-1111-1111-111111111111",
        "origin": origin if origin is not None else {"platform": "webui", "chat_id": "sess-a", "ui_session_id": "sess-a"},
        "turns": turns if turns is not None else [
            {"n": 1, "kind": "task", "started_at": _iso(time.time() - 300),
             "finished_at": _iso(time.time() - 100) if status not in ("queued", "running") else None,
             "exit_code": 0 if status == "completed" else None},
        ],
        "last_run": last_run if last_run is not None else {"launcher": "manual", "pid": os.getpid(), "started_at": _iso(time.time() - 300)},
        "wake": {"delivered": True, "enabled": True, "attempts": 1, "last_error": None},
        "delivery": delivery if delivery is not None else {"attempted": True, "ok": True, "channel": "webui:sess-a", "reason": "ok"},
        "final_result_chars": final_result_chars,
        "error": error,
    }
    (d / "job.json").write_text(json.dumps(job))
    tdir = d / "turns"
    tdir.mkdir(exist_ok=True)
    (tdir / "01.out").write_text("stream")
    return d, job


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def run_writer(tmp_path: Path, root: Path, stall_s="300", result_ttl="604800", pi_sessions=None,
               done_quiet_s=None, notable_window_s=None):
    out = tmp_path / "status.json"
    env = dict(os.environ)
    env.update({
        "PI_BRIDGE_HOME": str(root),
        "PI_LAMP_OUT": str(out),
        "PI_LAMP_CACHE": str(tmp_path / "cache.json"),
        "PI_LAMP_PIWEB": "0",
        "PI_LAMP_STALL_S": stall_s,
        "PI_LAMP_RESULT_TTL_S": result_ttl,
        "PI_LAMP_PI_SESSIONS": str(pi_sessions) if pi_sessions else str(tmp_path / "no-sessions"),
    })
    if done_quiet_s is not None:
        env["PI_LAMP_DONE_QUIET_S"] = str(done_quiet_s)
    if notable_window_s is not None:
        env["PI_LAMP_NOTABLE_WINDOW_S"] = str(notable_window_s)
    r = subprocess.run([_python3, str(WRITER)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(out.read_text())


JOB_OK = "pb-20260101T000000Z-aaaaaa"
JOB_B = "pb-20260101T000001Z-bbbbbb"
JOB_C = "pb-20260101T000002Z-cccccc"
JOB_D = "pb-20260101T000003Z-dddddd"
JOB_E = "pb-20260101T000004Z-eeeeee"


def test_running_view(tmp_path):
    root = tmp_path / "bridge"
    make_job(root, JOB_OK)  # last_run.pid = this test pid => alive
    data = run_writer(tmp_path, root)
    jobs = {j["job_id"]: j for j in data["jobs"]}
    assert jobs[JOB_OK]["view"] == "running"
    assert jobs[JOB_OK]["sessions"] == ["sess-a"]
    assert jobs[JOB_OK]["activity_age_s"] < 120
    assert jobs[JOB_OK]["turn_n"] == 1 and jobs[JOB_OK]["turn_kind"] == "task"


def test_stalled_view_with_stale_mtimes(tmp_path):
    root = tmp_path / "bridge"
    d, _ = make_job(root, JOB_OK, created=str(-3700), updated=str(-3600))  # alive runner, stale files
    old = time.time() - 3600
    for p in [d / "job.json", d / "turns" / "01.out"]:
        os.utime(p, (old, old))
    data = run_writer(tmp_path, root, stall_s="300")
    jobs = {j["job_id"]: j for j in data["jobs"]}
    assert jobs[JOB_OK]["view"] == "stalled"
    assert jobs[JOB_OK]["activity_age_s"] > 3000


def _dead_pid() -> int:
    """A pid this process can prove is gone (PermissionError would mean
    'alive' to the writer, so probing is the only safe way to pick one)."""
    for pid in range(999999, 40000, -1):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except OSError:
            continue
    raise AssertionError("no provably dead pid found")


def test_interrupted_when_runner_dead(tmp_path):
    root = tmp_path / "bridge"
    make_job(root, JOB_OK, last_run={"launcher": "manual", "pid": _dead_pid(),
                                     "started_at": _iso(time.time() - 300)})
    data = run_writer(tmp_path, root)
    jobs = {j["job_id"]: j for j in data["jobs"]}
    assert jobs[JOB_OK]["view"] == "interrupted"


def test_live_pi_session_jsonl_resets_stall(tmp_path):
    """Runner alive + stale job-dir mtimes, but pi's session JSONL is being
    appended to right now => running, not stalled."""
    root = tmp_path / "bridge"
    d, job = make_job(root, JOB_OK, created=str(-3700), updated=str(-3700))
    old = time.time() - 3600
    for p in [d / "job.json", d / "turns" / "01.out"]:
        os.utime(p, (old, old))
    sess = tmp_path / "sessions" / "--cwd--"
    sess.mkdir(parents=True)
    (sess / f"2026-10-10T00-00-00-000Z_{job['pi_session_id']}.jsonl").write_text("{}")
    data = run_writer(tmp_path, root, stall_s="300", pi_sessions=tmp_path / "sessions")
    jobs = {j["job_id"]: j for j in data["jobs"]}
    assert jobs[JOB_OK]["view"] == "running"
    assert jobs[JOB_OK]["activity_age_s"] < 5


def test_done_and_failed_views(tmp_path):
    root = tmp_path / "bridge"
    make_job(root, JOB_OK, status="completed", final_result_chars=1234)
    make_job(root, JOB_B, status="failed", error="boom",
             origin={"platform": "webui", "chat_id": "sess-b", "ui_session_id": "sess-b"},
             turns=[{"n": 1, "kind": "task", "started_at": _iso(time.time() - 300),
                     "finished_at": _iso(time.time() - 100), "exit_code": 1}])
    data = run_writer(tmp_path, root)
    jobs = {j["job_id"]: j for j in data["jobs"]}
    assert jobs[JOB_OK]["view"] == "done"
    assert jobs[JOB_OK]["delivery"]["ok"] is True
    assert jobs[JOB_B]["view"] == "failed"
    assert jobs[JOB_B]["exit_code"] == 1
    # active first, sorted by view order
    assert [j["view"] for j in data["jobs"]] == ["failed", "done"]


def test_terminal_ttl_prunes(tmp_path):
    root = tmp_path / "bridge"
    make_job(root, JOB_OK, status="completed", updated=str(-9 * 86400), created=str(-10 * 86400))
    make_job(root, JOB_B, status="completed", updated=str(-3600))
    data = run_writer(tmp_path, root, result_ttl="7 days".replace("7 days", "604800"))
    ids = {j["job_id"] for j in data["jobs"]}
    assert JOB_OK not in ids and JOB_B in ids


def test_non_webui_origin_excluded(tmp_path):
    root = tmp_path / "bridge"
    make_job(root, JOB_OK, origin={"platform": "telegram", "chat_id": "123"})
    data = run_writer(tmp_path, root)
    assert data["jobs"] == []


def test_no_secrets_leak(tmp_path):
    root = tmp_path / "bridge"
    make_job(root, JOB_OK)
    data = run_writer(tmp_path, root)
    raw = json.dumps(data)
    assert "SECRET task text" not in raw
    assert "/secret/cwd" not in raw


def test_error_paths_are_redacted(tmp_path):
    """status.json is served over HTTP: an error string must not carry the
    local filesystem layout."""
    root = tmp_path / "bridge"
    make_job(root, JOB_OK, status="failed",
             error=("pi exited with code 1; stderr tail: ENOENT: no such file "
                    "or directory, open '/home/u/.pi/agent/sessions/--home-u--/x.jsonl'"),
             turns=[{"n": 1, "kind": "task", "started_at": _iso(time.time() - 300),
                     "finished_at": _iso(time.time() - 100), "exit_code": 1}])
    data = run_writer(tmp_path, root)
    jobs = {j["job_id"]: j for j in data["jobs"]}
    err = jobs[JOB_OK]["error"]
    assert err and "ENOENT" in err
    assert "/home/" not in err and ".jsonl" not in err
    assert "/home/" not in json.dumps(data) and str(Path.home()) not in json.dumps(data)


def test_error_truncation_never_cuts_a_placeholder(tmp_path):
    """A very long path in `error` must be redacted and truncated without
    leaving a half-written "<path>" fragment or any path remnant."""
    root = tmp_path / "bridge"
    long_err = "open '" + "/home/u/" + "d" * 300 + "/secret.txt'"
    make_job(root, JOB_OK, status="failed", error=long_err,
             turns=[{"n": 1, "kind": "task", "started_at": _iso(time.time() - 300),
                     "finished_at": _iso(time.time() - 100), "exit_code": 1}])
    jobs = {j["job_id"]: j for j in run_writer(tmp_path, root)["jobs"]}
    err = jobs[JOB_OK]["error"]
    assert err and len(err) <= 200
    assert "secret.txt" not in err and "/home" not in err and "dddd" not in err
    assert not re.search(r"<p(?:a(?:t(?:h)?)?)?$", err), err


def test_terminal_duration_does_not_count_up(tmp_path):
    """A dead job whose last turn never recorded finished_at keeps a fixed
    duration instead of ticking forever in the card."""
    root = tmp_path / "bridge"
    make_job(root, JOB_OK, status="failed", created=str(-3600), updated=str(-1800),
             turns=[{"n": 1, "kind": "task", "started_at": _iso(time.time() - 3600),
                     "finished_at": None, "exit_code": 1}])
    first = {j["job_id"]: j for j in run_writer(tmp_path, root)["jobs"]}[JOB_OK]["duration_s"]
    second = {j["job_id"]: j for j in run_writer(tmp_path, root)["jobs"]}[JOB_OK]["duration_s"]
    assert first == second and 1700 <= first <= 1900


def test_authoritative_pi_session_file_is_used(tmp_path):
    """job.json's pi_session_file is trusted only when the file really sits
    inside the pi sessions tree; a path outside it is never statted."""
    root = tmp_path / "bridge"
    sess = tmp_path / "sessions" / "--cwd--"
    sess.mkdir(parents=True)
    d, job = make_job(root, JOB_OK, created=str(-3700), updated=str(-3700))
    old = time.time() - 3600

    def freeze():
        for p in [d / "job.json", d / "turns" / "01.out"]:
            os.utime(p, (old, old))

    freeze()
    live = sess / f"2026-10-10T00-00-00-000Z_{job['pi_session_id']}.jsonl"
    live.write_text("{}")
    # in-tree session file, referenced authoritatively -> activity is fresh
    (d / "job.json").write_text(json.dumps({**job, "pi_session_file": str(live)}))
    freeze()
    jobs = {j["job_id"]: j for j in run_writer(tmp_path, root, stall_s="300",
                                               pi_sessions=tmp_path / "sessions")["jobs"]}
    assert jobs[JOB_OK]["view"] == "running"

    # same session file but found only by the name glob (no pi_session_file) -> same answer
    (d / "job.json").write_text(json.dumps(job))
    freeze()
    jobs = {j["job_id"]: j for j in run_writer(tmp_path, root, stall_s="300",
                                               pi_sessions=tmp_path / "sessions")["jobs"]}
    assert jobs[JOB_OK]["view"] == "running"

    # a pi_session_file outside the pi sessions tree, hot right now, with
    # nothing left in the tree -> must be ignored, so the job looks stalled
    live.unlink()
    decoy = tmp_path / f"hot_{job['pi_session_id']}.jsonl"
    decoy.write_text("{}")
    (d / "job.json").write_text(json.dumps({**job, "pi_session_file": str(decoy)}))
    freeze()
    jobs = {j["job_id"]: j for j in run_writer(tmp_path, root, stall_s="300",
                                               pi_sessions=tmp_path / "sessions")["jobs"]}
    assert jobs[JOB_OK]["view"] == "stalled"


def test_writer_never_touches_bridge_state(tmp_path):
    """The writer is read-only with respect to PI_BRIDGE_HOME: every file name,
    size and mtime is unchanged after a run (this is what makes 'do not break
    other jobs' true)."""
    root = tmp_path / "bridge"
    make_job(root, JOB_OK)
    make_job(root, JOB_B, status="completed", updated=str(-60))

    def fingerprint():
        out = {}
        for p in sorted(root.rglob("*")):
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, int(st.st_mtime))
        return out

    before = fingerprint()
    run_writer(tmp_path, root)
    assert fingerprint() == before


def test_piweb_is_opt_in_and_local_only(tmp_path, monkeypatch):
    """PI_LAMP_PIWEB=0 (what the tests use) means no HTTP at all; with a config
    whose host is not private there is still no link and no request."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("pilamp_writer", WRITER)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setenv("PI_LAMP_PIWEB", "1")
    spec.loader.exec_module(mod)
    cfg = tmp_path / "pi-web-config.json"
    mod.PIWEB_CONFIG = cfg

    def base_for(host, port=8504):
        cfg.write_text(json.dumps({"host": host, "port": port}))
        mod._base_memo["done"] = False   # one read per process; reset per case
        return mod.piweb_base()

    assert base_for("8.8.8.8") is None
    for host in ("127.0.0.1", "192.168.2.10", "10.0.0.5", "172.16.5.9", "169.254.4.4", "::1"):
        assert base_for(host) == f"http://{'[' + host + ']' if ':' in host else host}:8504"
    # the one accepted name resolves to loopback itself, never via DNS
    assert base_for("localhost") == "http://127.0.0.1:8504"
    for host in ("0.0.0.0", "1.1.1.1", "8.8.8.8", "pi-web.example.com", "localhost.evil", ""):
        assert base_for(host) is None
    for port in ("8504", True, None, 0.5):
        assert base_for("127.0.0.1", port) is None
    assert base_for("127.0.0.1", 70000) == "http://127.0.0.1:70000"  # not our job to validate
    # loopback config + a job in an unknown cwd => no link, no crash, no request
    base_for("127.0.0.1", 1)
    budget = mod.Budget(0.001)
    job = {"job_id": JOB_OK, "status": "running", "cwd": str(tmp_path), "updated_at": _iso(time.time()),
           "pi_session_id": "11111111-1111-1111-1111-111111111111"}
    assert mod.piweb_view(job, {}, time.time(), budget) is None
    assert mod.Budget(0).expired() is True


def test_http_get_never_follows_redirects(tmp_path):
    """A 3xx from the local PI WEB must not turn into a request to a host the
    private-address guard never validated (both servers here are loopback; the
    redirect target's own hit counter proves we never connected to it)."""
    import importlib.util
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    spec = importlib.util.spec_from_file_location("pilamp_writer2", WRITER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    hits = {"target": 0}

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits["target"] += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"projects": []}')

        def log_message(self, *a):  # silence
            pass

    class Redirecter(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/x")
            self.end_headers()

        def log_message(self, *a):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    redir = ThreadingHTTPServer(("127.0.0.1", 0), Redirecter)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (target, redir)]
    for t in threads:
        t.start()
    try:
        base = f"http://127.0.0.1:{redir.server_port}"
        status, parsed = mod.http_get(base, "/api/projects", mod.Budget(5))
        assert status is None and parsed is None
        assert hits["target"] == 0, "redirect target was contacted"
        # and a plain failed read is a failed read, not an exception
        assert mod.http_get("http://127.0.0.1:1", "/api/projects", mod.Budget(5)) == (None, None)
        # a budget already spent issues nothing at all
        assert mod.http_get(base, "/api/projects", mod.Budget(-1)) == (None, None)
    finally:
        for s in (target, redir):
            s.shutdown()
            s.server_close()


def test_cache_is_shape_validated_and_private(tmp_path):
    """A corrupted/hand-edited cache must not wedge a tick, and the cache file
    (whose keys are local absolute paths) must not be world-readable."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("pilamp_writer3", WRITER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    cache_file = tmp_path / "cache.json"
    mod.CACHE_PATH = cache_file
    cache_file.write_text(json.dumps({
        "ok": {"ts": 1.0, "view": None},
        "bad_ts": {"ts": "soon", "view": {}},
        "bad_map": {"ts": 1.0, "map": "not-a-dict"},
        "bad_ttl": {"ts": 1.0, "ttl": "300"},
        "not_a_dict": [1, 2, 3],
    }), encoding="utf-8")
    loaded = mod.load_cache()
    assert set(loaded) == {"ok"}

    mod.save_cache({"k": {"ts": 2.0, "map": {"/home/u": {"id": "p1"}}}})
    assert (cache_file.stat().st_mode & 0o077) == 0

    # projects_map survives a poisoned-by-hand cache without raising
    assert mod.projects_map("http://127.0.0.1:1", mod.Budget(0.2),
                            {"__projects__": {"ts": 0.0, "map": "oops"}}, 0.0) == {}


def test_negative_answers_expire_but_links_survive(tmp_path):
    """A transient PI WEB failure must not erase a working workspace id, and a
    negative answer must expire (retry) instead of refreshing forever."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("pilamp_writer4", WRITER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    now = 1_000.0
    cache = {"__ws__p1": {"ts": now - 400.0, "id": "w1"}}   # expired for META_TTL_S
    # budget spent -> the stale answer is still returned
    assert mod.workspace_id("http://127.0.0.1:1", "p1", "/home/u",
                            mod.Budget(-1), cache, now) == "w1"
    cache["__ws__p1"]["ts"] = now - 400.0
    # a real failure keeps the old id but shortens its life to NEG_TTL_S
    wid = mod.workspace_id("http://127.0.0.1:1", "p1", "/home/u",
                           mod.Budget(0.3), cache, now)
    assert wid == "w1"
    assert cache["__ws__p1"]["ttl"] == mod.NEG_TTL_S
    assert mod._cache_get(cache, "__ws__p1", mod.META_TTL_S, now + mod.NEG_TTL_S + 1) is None


def test_corrupt_job_dir_ignored(tmp_path):
    root = tmp_path / "bridge"
    make_job(root, JOB_OK)
    bad = root / "jobs" / JOB_B
    bad.mkdir(parents=True)
    (bad / "job.json").write_text("{not json")
    junk = root / "jobs" / "not-a-job"
    junk.mkdir(parents=True)
    data = run_writer(tmp_path, root)
    assert {j["job_id"] for j in data["jobs"]} == {JOB_OK}


# ───────────────────────────────── finished_at / UI grouping (writer 1.1) ──

# what a writer-1.0 consumer reads; the 1.1 fields are strictly additive
V1_JOB_FIELDS = {"job_id", "view", "status", "sessions", "created_at", "updated_at",
                 "duration_s", "turn_n", "turn_kind", "turn_started_at", "turns_total",
                 "exit_code", "error", "delivery", "wake_delivered", "result_chars"}


def _turns(finished_off=None, n=1, kind="task", exit_code=0, started_off=-300):
    return [{"n": n, "kind": kind, "started_at": _iso(time.time() + started_off),
             "finished_at": _iso(time.time() + finished_off) if finished_off is not None else None,
             "exit_code": exit_code}]


def test_new_fields_are_additive_for_existing_consumers(tmp_path):
    """Old readers (or an old cached snapshot) must keep working: every 1.0 key
    is still there, and only finished_at / finished_age_s / group were added."""
    root = tmp_path / "bridge"
    make_job(root, JOB_OK)                                  # running
    make_job(root, JOB_B, status="completed", updated=str(-3600),
             turns=_turns(finished_off=-3600))
    data = run_writer(tmp_path, root)
    assert {"generated_at", "writer_version", "stall_threshold_s", "jobs"} <= set(data)
    assert (tuple(int(x) for x in data["writer_version"].split(".")) >= (1, 1, 0))
    jobs = {j["job_id"]: j for j in data["jobs"]}
    assert V1_JOB_FIELDS <= set(jobs[JOB_OK]) and V1_JOB_FIELDS <= set(jobs[JOB_B])
    assert {"finished_at", "finished_age_s", "group"} <= set(jobs[JOB_B])


def test_active_job_has_no_finished_at(tmp_path):
    root = tmp_path / "bridge"
    make_job(root, JOB_OK)
    j = {x["job_id"]: x for x in run_writer(tmp_path, root)["jobs"]}[JOB_OK]
    assert j["group"] == "active"
    assert j["finished_at"] is None and j["finished_age_s"] is None


def test_done_finished_at_comes_from_the_last_turn(tmp_path):
    root = tmp_path / "bridge"
    made = _iso(time.time() - 3600)
    make_job(root, JOB_OK, status="completed", updated=str(-3600),
             turns=[{"n": 2, "kind": "task", "started_at": _iso(time.time() - 4000),
                     "finished_at": made, "exit_code": 0}])
    j = {x["job_id"]: x for x in run_writer(tmp_path, root)["jobs"]}[JOB_OK]
    assert j["finished_at"] == made                     # exact turn timestamp
    assert 3500 <= j["finished_age_s"] <= 3700
    assert j["group"] == "recent"                       # finished an hour ago: still a row


def test_finished_at_falls_back_to_updated_at(tmp_path):
    """A runner killed mid-turn writes no finished_at: updated_at is the only
    honest end-of-work timestamp, and it must drive the quiet window too."""
    root = tmp_path / "bridge"
    make_job(root, JOB_OK, status="failed", created=str(-5 * 3600), updated=str(-4 * 3600),
             turns=_turns(finished_off=None, exit_code=1))
    # compare against what was actually written, never against a clock read
    # again at assert time (a writer tick crossing a second boundary made the
    # old equality flaky)
    written = json.loads((root / "jobs" / JOB_OK / "job.json").read_text())["updated_at"]
    j = {x["job_id"]: x for x in run_writer(tmp_path, root)["jobs"]}[JOB_OK]
    assert j["finished_at"] == written
    assert 14300 <= j["finished_age_s"] <= 14500
    assert j["group"] == "recent"                       # a failure is never quiet


def test_done_leaves_the_card_after_three_hours(tmp_path):
    root = tmp_path / "bridge"
    make_job(root, JOB_OK, status="completed", updated=str(-5 * 3600),
             turns=_turns(finished_off=-5 * 3600))
    make_job(root, JOB_B, status="completed", updated=str(-3500), origin={
        "platform": "webui", "chat_id": "sess-b", "ui_session_id": "sess-b"},
        turns=_turns(finished_off=-3500))
    jobs = {j["job_id"]: j for j in run_writer(tmp_path, root)["jobs"]}
    assert jobs[JOB_OK]["group"] == "quiet"             # 5 h old: counted, not listed
    assert jobs[JOB_B]["group"] == "recent"             # 1 h old: still a row
    # ...and the threshold is configuration, not a hard-coded constant
    jobs = {j["job_id"]: j for j in run_writer(tmp_path, root, done_quiet_s="600")["jobs"]}
    assert jobs[JOB_B]["group"] == "quiet"


def test_failures_are_never_quiet_but_cancelled_is(tmp_path):
    root = tmp_path / "bridge"
    make_job(root, JOB_OK, status="failed", updated=str(-5 * 3600),
             error="boom", turns=_turns(finished_off=-5 * 3600, exit_code=1))
    make_job(root, JOB_B, status="cancelled", updated=str(-5 * 3600),
             turns=_turns(finished_off=-5 * 3600))
    jobs = {j["job_id"]: j for j in run_writer(tmp_path, root)["jobs"]}
    assert jobs[JOB_OK]["group"] == "recent"   # acknowledge is the only hide-off
    assert jobs[JOB_B]["group"] == "quiet"     # nothing to look at, three hours on


def test_terminal_jobs_older_than_a_day_stop_counting(tmp_path):
    root = tmp_path / "bridge"
    make_job(root, JOB_OK, status="completed", updated=str(-30 * 3600),
             turns=_turns(finished_off=-30 * 3600))
    make_job(root, JOB_B, status="failed", updated=str(-30 * 3600), error="old",
             turns=_turns(finished_off=-30 * 3600, exit_code=1))
    jobs = {j["job_id"]: j for j in run_writer(tmp_path, root)["jobs"]}
    # still in status.json (other consumers, RESULT_TTL_S=7d), just not on the lamp
    assert jobs[JOB_OK]["group"] == "aged" and jobs[JOB_B]["group"] == "aged"
    jobs = {j["job_id"]: j for j in run_writer(tmp_path, root, notable_window_s="3600")["jobs"]}
    assert jobs[JOB_OK]["group"] == "aged"


def test_groups_order_the_snapshot_active_first(tmp_path):
    """One chat, four jobs: running + a fresh failure + a fresh done + a quiet
    done.  The badge question is 'what is running', so active sorts first and
    the quiet one sorts last — the MAX_JOBS cut then eats the boring tail."""
    root = tmp_path / "bridge"
    make_job(root, JOB_OK)                                             # running
    make_job(root, JOB_B, status="failed", updated=str(-7200), error="boom",
             turns=_turns(finished_off=-7200, exit_code=1))
    make_job(root, JOB_C, status="completed", updated=str(-3600),
             turns=_turns(finished_off=-3600))
    make_job(root, JOB_D, status="completed", updated=str(-5 * 3600),
             turns=_turns(finished_off=-5 * 3600))
    make_job(root, JOB_E, origin={"platform": "telegram", "chat_id": "1"})  # invisible
    data = run_writer(tmp_path, root)
    assert [(j["job_id"], j["group"]) for j in data["jobs"]] == [
        (JOB_OK, "active"), (JOB_B, "recent"), (JOB_C, "recent"), (JOB_D, "quiet")]
    assert {j["sessions"][0] for j in data["jobs"]} == {"sess-a"}


def test_vanished_runner_never_counts_up_in_the_card(tmp_path):
    """status=running but the runner is gone => view `interrupted`, which the
    card files under Problems: its duration must be frozen at the last update,
    not tick away under a row that reads "30m ago"."""
    root = tmp_path / "bridge"
    make_job(root, JOB_OK, created=str(-3 * 3600), updated=str(-1800),
             last_run={"launcher": "manual", "pid": _dead_pid(),
                       "started_at": _iso(time.time() - 3600)},
             turns=_turns(finished_off=None))
    first = {j["job_id"]: j for j in run_writer(tmp_path, root)["jobs"]}[JOB_OK]
    second = {j["job_id"]: j for j in run_writer(tmp_path, root)["jobs"]}[JOB_OK]
    assert first["view"] == "interrupted" and first["group"] == "recent"
    assert first["finished_at"] == second["finished_at"]
    assert first["duration_s"] == second["duration_s"]
    assert 8900 <= first["duration_s"] <= 9100   # created 3h ago, last update 30m ago
