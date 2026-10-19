"""V1.5 webui delivery spool: pi_bridge/webui_event.py unit tests.

Contract under test (docs/WEBUI_COMPLETION_EVENT_SPEC.md): the runner never
runs a Hermes CLI for a webui origin any more.  It writes ONE durable request
next to the job (`jobs/<job_id>/webui_delivery.json`) and the bridge's Hermes
plugin drains that spool in-process.  This module is the shared spool format
+ the native event shape, so everything here is pure file/field logic against
a temporary PI_BRIDGE_HOME -- no subprocess, no Hermes, no network.
"""
import json
import os
import time
from pathlib import Path

import pytest

from pi_bridge import deliver, state, webui_event

JOB_ID = "pb-20260101T000000Z-abcdef"
JOB_ID_2 = "pb-20260101T000001Z-123456"
SID = "ee58f3039666"
NAME = "webui_delivery.json"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A bridge home with one finished job (no runner, no hermes)."""
    h = tmp_path / "bridge-home"
    (h / "jobs" / JOB_ID).mkdir(parents=True)
    (h / "jobs" / JOB_ID_2).mkdir(parents=True)
    for jid in (JOB_ID, JOB_ID_2):
        state.write_text_atomic(
            h / "jobs" / jid / "job.json",
            json.dumps({"job_id": jid, "status": "completed", "task": "t",
                        "turns": [{"n": 1}], "final_result": "FAKEOK",
                        "delivery": {}}))
    monkeypatch.setenv("PI_BRIDGE_HOME", str(h))
    return h


def jdir(job_id: str = JOB_ID) -> Path:
    return state.jobs_root() / job_id


def req_file(job_id: str = JOB_ID) -> Path:
    return jdir(job_id) / NAME


# ---------------------------------------------------------------------------
# write / read / supersede
# ---------------------------------------------------------------------------

def test_write_and_read_roundtrip(home):
    req = webui_event.write_request(JOB_ID, ui_session_id=SID, turn=1,
                                    text="the outcome text")
    assert req_file().is_file()
    got = webui_event.read_request(jdir())
    assert got["job_id"] == JOB_ID
    assert got["ui_session_id"] == SID
    assert got["turn"] == 1
    assert got["text"] == "the outcome text"
    assert got["state"] == "pending"          # delivery state machine entry
    assert got["status"] == "completed"       # job outcome
    assert got["error"] is None
    assert got["attempts"] == 0
    assert got["created_at"] and got["updated_at"]
    assert got["_dir"] == str(jdir())         # read_request's private hint
    assert req["job_id"] == JOB_ID


def test_write_is_atomic_and_leaves_no_temp_files(home):
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=1, text="x")
    leftovers = [p.name for p in jdir().iterdir()
                 if p.name.startswith(".tmp-") or p.name.endswith(".tmp")]
    assert leftovers == []


def test_read_request_is_defensive(home):
    assert webui_event.read_request(jdir()) is None          # nothing yet
    req_file().write_text("{not json", "utf-8")
    assert webui_event.read_request(jdir()) is None
    req_file().write_text(json.dumps({"no_job_id": 1}), "utf-8")
    assert webui_event.read_request(jdir()) is None


def test_second_turn_supersedes_the_first(home):
    """One active request per job: a newer turn overwrites the file."""
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=1,
                              text="first outcome")
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=2,
                              text="second outcome")
    got = webui_event.read_request(jdir())
    assert got["turn"] == 2
    assert got["text"] == "second outcome"
    assert got["state"] == "pending"          # a new turn is a new delivery
    assert list(jdir().glob(NAME + ".inflight.*")) == []


# ---------------------------------------------------------------------------
# claim / release
# ---------------------------------------------------------------------------

def test_claim_is_one_shot_per_process(home):
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=1, text="x")
    req = webui_event.read_request(jdir())

    first = webui_event.claim(req)
    assert first is not None
    assert first["_inflight_path"].endswith(f".inflight.{os.getpid()}")
    assert Path(first["_inflight_path"]).is_file()
    # the canonical file moved away, so nobody else can claim it
    assert not req_file().exists()
    assert webui_event.claim(req) is None
    assert webui_event.claim(first) is None
    # and the request is simply invisible while claimed
    assert webui_event.iter_requests() == []


def test_claim_survives_across_reader_instances(home):
    """Two independent scans of the same spool still yield one winner."""
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=1, text="x")
    a, b = webui_event.iter_requests()[0], webui_event.iter_requests()[0]
    assert webui_event.claim(a) is not None
    assert webui_event.claim(b) is None


def test_release_transitions_and_drops_inflight(home):
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=1, text="x")
    claimed = webui_event.claim(webui_event.read_request(jdir()))
    kept = webui_event.release(claimed, state_value="injected")
    assert kept["state"] == "injected"
    assert req_file().is_file()
    assert json.loads(req_file().read_text("utf-8"))["state"] == "injected"
    assert list(jdir().glob(NAME + ".inflight.*")) == []
    # no private scan hints ever reach the file
    assert "_dir" not in json.loads(req_file().read_text("utf-8"))
    assert "_inflight_path" not in json.loads(req_file().read_text("utf-8"))

    claimed = webui_event.claim(webui_event.read_request(jdir()))
    webui_event.release(claimed, state_value="delivered", reason="ok")
    done = json.loads(req_file().read_text("utf-8"))
    assert done["state"] == "delivered" and done["reason"] == "ok"
    assert done["updated_at"]
    assert list(jdir().glob(NAME + ".inflight.*")) == []


def test_release_drops_the_internal_keys(home):
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=1, text="x")
    claimed = webui_event.claim(webui_event.read_request(jdir()))
    webui_event.release(claimed, state_value="abandoned",
                        reason="native-delivery-exhausted")
    on_disk = json.loads(req_file().read_text("utf-8"))
    assert not [k for k in on_disk if k.startswith("_")]
    assert on_disk["attempts"] == 0           # bumping is explicit, not implied


# ---------------------------------------------------------------------------
# scan visibility
# ---------------------------------------------------------------------------

def test_iter_requests_visibility(home):
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=1, text="p")
    assert [r["job_id"] for r in webui_event.iter_requests()] == [JOB_ID]

    claimed = webui_event.claim(webui_event.read_request(jdir()))
    webui_event.release(claimed, state_value="injected")
    assert [r["job_id"] for r in webui_event.iter_requests()] == [JOB_ID]

    claimed = webui_event.claim(webui_event.read_request(jdir()))
    webui_event.release(claimed, state_value="delivered", reason="ok")
    assert webui_event.iter_requests() == []      # terminal: stop scanning


def test_iter_requests_skips_dirs_without_jobs_or_requests(home):
    empty = state.jobs_root() / "pb-20260101T000002Z-000000"
    empty.mkdir()
    (empty / NAME).write_text(json.dumps({"job_id": "x"}), "utf-8")
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=1, text="p")
    # the job-less directory is skipped even though it holds a request file
    assert [r["job_id"] for r in webui_event.iter_requests()] == [JOB_ID]


def test_stale_inflight_is_stolen_back_but_fresh_is_not(home):
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=1, text="x")
    inflight = webui_event.claim(webui_event.read_request(jdir()))
    path = Path(inflight["_inflight_path"])

    # a live claim by another (or our own crashed-but-recent) poller: skip
    assert webui_event.iter_requests() == []

    os.utime(path, (time.time() - webui_event.MAX_INFLIGHT_AGE_S - 5,) * 2)
    stale = webui_event.iter_requests()
    assert len(stale) == 1
    assert stale[0]["job_id"] == JOB_ID
    assert stale[0]["_inflight_path"] == str(path)


# ---------------------------------------------------------------------------
# the native completion event shape
# ---------------------------------------------------------------------------

def test_build_event_completed_shape(home):
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=3,
                              text="Pi-задача ...\n\nthe outcome",
                              status="completed")
    req = webui_event.read_request(jdir())
    evt = webui_event.build_event(req, task_preview="do the thing")
    assert evt["type"] == "async_delegation"
    assert evt["delegation_id"] == f"pi-bridge-{JOB_ID}-t3"
    assert evt["origin_ui_session_id"] == SID
    assert evt["session_key"] == SID
    assert evt["parent_session_id"] == SID
    assert evt["origin_session_id"] == ""
    assert evt["is_batch"] is False
    assert evt["status"] == "completed"
    assert evt["summary"] == req["text"]
    assert evt["error"] is None
    assert evt["goal"] == f"Pi job {JOB_ID} (turn 3): do the thing"
    for k in ("dispatched_at", "completed_at"):
        assert isinstance(evt[k], float) and evt[k] > 0


def test_build_event_failed_carries_the_error(home):
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=1,
                              text="header\n\nboom", status="failed",
                              error="pi exited 3")
    evt = webui_event.build_event(webui_event.read_request(jdir()))
    assert evt["status"] == "failed"
    assert evt["error"] == "pi exited 3"
    assert evt["summary"] == "header\n\nboom"


def test_build_event_failed_without_error_has_a_default(home):
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=1, text="x",
                              status="failed", error=None)
    evt = webui_event.build_event(webui_event.read_request(jdir()))
    assert evt["error"] == "the Pi turn failed"


def test_delegation_id_is_stable_and_turn_scoped(home):
    assert webui_event.delegation_id(JOB_ID, 1) == f"pi-bridge-{JOB_ID}-t1"
    assert webui_event.delegation_id(JOB_ID, 1) == \
        webui_event.delegation_id(JOB_ID, 1)
    assert webui_event.delegation_id(JOB_ID, 2) != \
        webui_event.delegation_id(JOB_ID, 1)
    assert webui_event.delegation_id(JOB_ID, 1) != \
        webui_event.delegation_id(JOB_ID_2, 1)


def test_build_event_reuses_the_same_delegation_id_on_reinjection(home):
    """Re-injection must NOT create a second acceptance turn: the ledger claim
    is keyed on delegation_id, so it has to be byte-stable."""
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=2, text="x")
    req = webui_event.read_request(jdir())
    ids = {webui_event.build_event(req)["delegation_id"] for _ in range(3)}
    assert ids == {f"pi-bridge-{JOB_ID}-t2"}


def test_ledger_row_shape(home):
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=1, text="x")
    evt = webui_event.build_event(webui_event.read_request(jdir()))
    row = webui_event.ledger_row(evt)
    assert len(row) == 9
    assert row[0] == f"pi-bridge-{JOB_ID}-t1"          # delegation_id
    assert row[1] == SID                               # origin_session
    assert row[2] == SID                               # origin_ui_session_id
    assert row[3] == SID                               # parent_session_id
    assert row[4] == "completed"                       # ledger state
    assert isinstance(row[5], float)                   # dispatched_at
    assert isinstance(row[6], float)                   # completed_at
    assert isinstance(row[7], float)                   # updated_at
    assert json.loads(row[8])["delegation_id"] == row[0]


# ---------------------------------------------------------------------------
# attempts budget
# ---------------------------------------------------------------------------

def test_attempt_budget(home):
    webui_event.write_request(JOB_ID, ui_session_id=SID, turn=1, text="x")
    req = webui_event.read_request(jdir())
    assert webui_event.attempts_exhausted(req) is False
    bumped = webui_event.bump_attempt(req)
    assert bumped["attempts"] == 1 and req["attempts"] == 0   # pure
    assert webui_event.bump_attempt(bumped)["attempts"] == 2
    tired = dict(req, attempts=webui_event.MAX_INJECTION_ATTEMPTS)
    assert webui_event.attempts_exhausted(tired) is True


# ---------------------------------------------------------------------------
# hosting gate
# ---------------------------------------------------------------------------

def write_lease(hermes_home, entries):
    d = Path(hermes_home) / "runtime"
    d.mkdir(parents=True, exist_ok=True)
    (d / "active_sessions.json").write_text(
        json.dumps({"entries": entries}), "utf-8")
    return d


def test_lease_owner_and_may_host(home, tmp_path):
    hh = tmp_path / "hermes-home"
    write_lease(hh, [{"session_id": SID, "pid": os.getpid(),
                      "surface": "webui"},
                     {"session_id": "other-sid", "pid": 999999,
                      "surface": "telegram"}])

    assert webui_event.lease_owner(SID, hermes_home=hh) == \
        (os.getpid(), "webui")
    assert webui_event.may_host_session(SID, loaded_modules=set(),
                                        hermes_home=hh) is True
    # a lease held by a DIFFERENT process -> this process must never inject
    assert webui_event.lease_owner("other-sid", hermes_home=hh) == (999999,
                                                                    "telegram")
    assert webui_event.may_host_session("other-sid", loaded_modules=set(),
                                        hermes_home=hh) is False
    # an unknown session has no lease at all
    assert webui_event.lease_owner("nope", hermes_home=hh) is None
    assert webui_event.may_host_session("nope", loaded_modules=set(),
                                        hermes_home=hh) is False


def test_may_host_is_true_for_the_webui_backend_without_any_lease(tmp_path):
    assert webui_event.may_host_session(
        SID, loaded_modules={"tui_gateway.server"},
        hermes_home=tmp_path / "missing-hermes-home") is True
    # the Hermes WebUI server (api/background_process drain thread) is the
    # other legitimate native consumer
    assert webui_event.may_host_session(
        SID, loaded_modules={"api.background_process"},
        hermes_home=tmp_path / "missing-hermes-home") is True


def test_may_host_is_false_in_the_gateway_runner(tmp_path):
    """The gateway imports tui_gateway.server too (its hosted-room worker)
    and its delegation watcher preflights foreign completions against the
    session DB and terminally DROPS them: the gate must stay shut in the
    gateway even when it otherwise looks exactly like the backend."""
    mods = {"tui_gateway.server", "gateway.run"}
    assert webui_event.may_host_session(
        SID, loaded_modules=mods,
        hermes_home=tmp_path / "missing-hermes-home") is False
    hh = tmp_path / "hermes-home"
    write_lease(hh, [{"session_id": SID, "pid": os.getpid(),
                      "surface": "webui"}])
    # gateway.run denial outranks even a live lease held by THIS pid
    assert webui_event.may_host_session(SID, loaded_modules=mods,
                                        hermes_home=hh) is False
    # the same lease without gateway.run still hosts (a CLI that legitimately
    # owns the session can complete a queued delivery)
    assert webui_event.may_host_session(
        SID, loaded_modules={"tui_gateway.server"}, hermes_home=hh) is True


def test_lease_home_honours_hermes_home_env(tmp_path, monkeypatch):
    hh = tmp_path / "profile-home"
    write_lease(hh, [{"session_id": SID, "pid": os.getpid(),
                      "surface": "webui"}])
    monkeypatch.setenv("HERMES_HOME", str(hh))
    assert webui_event.lease_owner(SID) == (os.getpid(), "webui")
    monkeypatch.delenv("HERMES_HOME")
    assert webui_event.lease_owner(SID) is None


def test_may_host_is_false_when_the_registry_is_unreadable(tmp_path):
    hh = tmp_path / "hermes-home"
    assert webui_event.may_host_session(SID, loaded_modules=set(),
                                        hermes_home=hh) is False
    write_lease(hh, [{"session_id": SID, "pid": "not-a-pid"}])
    assert webui_event.lease_owner(SID, hermes_home=hh) is None
    (hh / "runtime" / "active_sessions.json").write_text("{ broken", "utf-8")
    assert webui_event.may_host_session(SID, loaded_modules=set(),
                                        hermes_home=hh) is False


# ---------------------------------------------------------------------------
# v0.4.2 process-role gate (pure twin of the plugin's role-aware decision)
# ---------------------------------------------------------------------------

GW_ARGV = ["/h/venv/bin/python", "-m", "hermes_cli.main", "gateway", "run"]


def test_webui_role_from_env_beats_a_late_gateway_run_import(tmp_path):
    """The production regression: a webui host identified purely by
    HERMES_WEBUI_STATE_DIR (argv = pytest's) must still host after
    gateway.run has been lazily imported."""
    mods = {"gateway.run", "tui_gateway.server"}
    assert webui_event.host_decision(
        SID, loaded_modules=mods, hermes_home=tmp_path / "missing",
        argv=["/venv/bin/pytest", "tests"],
        environ={"HERMES_WEBUI_STATE_DIR": "/srv/webui"},
        main_file="/venv/bin/pytest") == (True, "webui-positive")
    assert webui_event.process_role(
        argv=["/venv/bin/pytest", "tests"],
        environ={"HERMES_WEBUI_AGENT_DIR": "/srv/agent"},
        main_file="/venv/bin/pytest") == "webui"
    # api.background_process is hermes-webui-exclusive: a positive module
    # signal survives the late gateway.run import as well
    assert webui_event.host_decision(
        SID, loaded_modules=mods | {"api.background_process"},
        hermes_home=tmp_path / "missing",
        argv=["/venv/bin/pytest"], environ={},
        main_file="/venv/bin/pytest") == (True, "webui-positive")


def test_gateway_argv_vetoes_before_any_lazy_import(tmp_path):
    """role 'gateway' from argv alone -> never inject, even with a lease
    held by THIS pid and no gateway.run imported yet."""
    hh = tmp_path / "hermes-home"
    write_lease(hh, [{"session_id": SID, "pid": os.getpid(),
                      "surface": "webui"}])
    for argv in (GW_ARGV,
                 ["/h/venv/bin/hermes", "gateway", "run"],
                 ["/h/venv/bin/python", "-m", "gateway.run"]):
        assert webui_event.host_decision(
            SID, loaded_modules={"tui_gateway.server"},
            hermes_home=hh, argv=argv, environ={},
            main_file="/h/venv/bin/python") == (False, "gateway-runner")


def test_tui_backend_role_hosts(tmp_path):
    assert webui_event.process_role(
        argv=["/h/venv/bin/python", "-m", "tui_gateway.entry"],
        environ={}, main_file="") == "tui_backend"
    assert webui_event.host_decision(
        SID, loaded_modules=set(), hermes_home=tmp_path / "missing",
        argv=["/h/venv/bin/python", "-m", "tui_gateway"], environ={},
        main_file="") == (True, "tui-backend")


def test_may_host_session_and_host_decision_agree_on_wrappers(tmp_path):
    hh = tmp_path / "hermes-home"
    write_lease(hh, [{"session_id": SID, "pid": os.getpid(),
                      "surface": "webui"}])
    assert webui_event.may_host_session(
        SID, loaded_modules=set(), hermes_home=hh, argv=[], environ={},
        main_file="") is True                       # lease-self
    assert webui_event.may_host_session(
        SID, loaded_modules={"gateway.run"}, hermes_home=hh, argv=[],
        environ={}, main_file="") is False          # gateway-run-loaded


# ---------------------------------------------------------------------------
# v0.4.2 attempt-budget cap: env override, garbage fallback, patchable
# ---------------------------------------------------------------------------

def test_max_attempts_env_override(monkeypatch):
    monkeypatch.setenv("PI_BRIDGE_MAX_INJECTION_ATTEMPTS", "5")
    assert webui_event._max_attempts_default() == 5
    monkeypatch.setenv("PI_BRIDGE_MAX_INJECTION_ATTEMPTS", "garbage")
    assert webui_event._max_attempts_default() == 1200
    monkeypatch.setenv("PI_BRIDGE_MAX_INJECTION_ATTEMPTS", "0")
    assert webui_event._max_attempts_default() == 1200
    monkeypatch.delenv("PI_BRIDGE_MAX_INJECTION_ATTEMPTS", raising=False)
    assert webui_event._max_attempts_default() == 1200


def test_attempts_cap_constant_is_patchable_for_tests(monkeypatch, home):
    monkeypatch.setattr(webui_event, "MAX_INJECTION_ATTEMPTS", 3)
    assert webui_event.attempts_exhausted({"attempts": 2}) is False
    assert webui_event.attempts_exhausted({"attempts": 3}) is True


# ---------------------------------------------------------------------------
# job.json bookkeeping written by the runner / the poller
# ---------------------------------------------------------------------------

def test_record_queued_marks_a_non_subprocess_delivery(home):
    webui_event.record_queued(JOB_ID, f"webui:{SID}")
    job = state.load_job(JOB_ID)
    assert job["delivery"] == {
        "attempted": True, "ok": False, "channel": f"webui:{SID}",
        "kind": "webui-event", "reason": "queued", "error": None,
        "turn": 1, "at": job["delivery"]["at"]}
    # the job status is never touched by delivery bookkeeping
    assert job["status"] == "completed"


def test_record_ack_writes_the_terminal_delivery(home):
    webui_event.record_queued(JOB_ID, f"webui:{SID}")
    webui_event.record_ack(JOB_ID, ok=True, reason="ok")
    d = state.load_job(JOB_ID)["delivery"]
    assert (d["attempted"], d["ok"], d["kind"], d["reason"],
            d["channel"]) == (True, True, "webui-event", "ok", f"webui:{SID}")
    webui_event.record_ack(JOB_ID, ok=False, reason="native-delivery-exhausted",
                           error="X" * 900)
    d = state.load_job(JOB_ID)["delivery"]
    assert (d["attempted"], d["ok"], d["reason"]) == (True, False,
                                                     "native-delivery-exhausted")
    assert len(d["error"]) == 300


def test_record_ack_never_raises(home):
    # unknown job id: a delivery ack is data, never an error for the caller
    webui_event.record_ack("pb-19990101T000000Z-ffffff", ok=False,
                           reason="nope")


# ---------------------------------------------------------------------------
# deliver.plan(): the webui branch is pure spooling, never a CLI
# ---------------------------------------------------------------------------

def _job(**origin):
    return {"job_id": JOB_ID, "status": "completed", "task": "t",
            "turns": [{}], "final_result": "systemctl restart nginx && rm -rf /",
            "origin": origin}


def test_plan_webui_is_an_event_not_a_subprocess(home):
    p = deliver.plan(_job(platform="webui", ui_session_id=SID),
                     "/usr/local/bin/hermes", "text")
    assert p["kind"] == "webui-event"
    assert p["argv"] is None                  # NO argv: nothing can be run
    assert p["channel"] == f"webui:{SID}"
    assert p["reason"] is None


def test_plan_webui_does_not_need_a_hermes_binary(home):
    for binary in ("", "/does/not/exist/hermes", "hermes"):
        p = deliver.plan(_job(platform="webui", ui_session_id=SID), binary,
                         "text")
        assert p["kind"] == "webui-event", binary
        assert p["argv"] is None and p["reason"] is None


def test_plan_webui_falls_back_to_chat_id_for_the_session(home):
    p = deliver.plan(_job(platform="webui", chat_id=SID), "", "text")
    assert p["kind"] == "webui-event" and p["channel"] == f"webui:{SID}"


def test_plan_webui_with_an_unusable_session_id_delivers_nowhere(home):
    for bad in ("bad id", "", "a" * 200):
        p = deliver.plan(_job(platform="webui", ui_session_id=bad), "", "text")
        assert p["kind"] is None and p["argv"] is None, bad
        assert p["reason"] == "no-webui-session-id", bad
