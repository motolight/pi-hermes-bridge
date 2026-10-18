"""Parity between pi_bridge/webui_event.py and the plugin's embedded mirror.

The plugin is copied into ~/.hermes/plugins/pi-worker/ and cannot import the
bridge venv, so plugin/__init__.py carries a deliberate stdlib-only copy of
the spool format, the event shape and the file protocol.  Two implementations
of one on-disk contract drift unless something pins them, and a drifted pair
means the runner writes files the poller cannot read (a silently undelivered
completion).  This file is that pin: shared fixtures, driven through both
implementations, compared field by field -- including a request that is
claimed by one and released by the other.  v0.4.1 additionally pins the
two former divergences: `goal` is read verbatim from the spool request by
both implementations, and `created_at` is parsed as a UTC instant in both
(no time.mktime anywhere), so `dispatched_at` agrees on any host timezone.
"""
import contextlib
import importlib.util
import json
import os
import queue
import time
from pathlib import Path

import pytest

from conftest import REPO
from pi_bridge import state, webui_event

JOB_ID = "pb-20260101T000000Z-abcdef"
JOB_ID_2 = "pb-20260101T000001Z-bbccee"
SID = "ee58f3039666"


def _load_plugin(name="pi_worker_plugin_parity"):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "plugin" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def utc(monkeypatch):
    """Pin the host timezone so the two epoch parsers are comparable."""
    old = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    if old is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", old)
    time.tzset()


@pytest.fixture()
def pair(tmp_path, monkeypatch):
    """(bridge module, plugin module) over one temp PI_BRIDGE_HOME."""
    home = tmp_path / "bridge-home"
    for jid in (JOB_ID, JOB_ID_2):
        (home / "jobs" / jid).mkdir(parents=True)
        state.write_text_atomic(
            home / "jobs" / jid / "job.json",
            json.dumps({"job_id": jid, "status": "completed", "task": "t",
                        "turns": [{"n": 1}], "final_result": "ok",
                        "delivery": {}}))
    monkeypatch.setenv("PI_BRIDGE_HOME", str(home))
    return webui_event, _load_plugin()


def req_fixture(we, turn=1, text="Pi-задача ...\n\nthe outcome",
                status="completed", error=None):
    return we.write_request(JOB_ID, ui_session_id=SID, turn=turn, text=text,
                            status=status, error=error,
                            goal=f"Pi job {JOB_ID} (turn {turn}): do it")


# ---------------------------------------------------------------------------
# shared constants (the file protocol itself)
# ---------------------------------------------------------------------------

def test_spool_constants_match(pair):
    we, mod = pair
    assert we.REQUEST_NAME == mod._request_name == "webui_delivery.json"
    assert we.MAX_INFLIGHT_AGE_S == mod._WEBUI_INFLIGHT_STALE_S
    assert we.MAX_INJECTION_ATTEMPTS == mod._WEBUI_MAX_ATTEMPTS
    assert tuple(we.BACKEND_MARKERS) == tuple(mod._BACKEND_MARKERS)


def test_delegation_id_is_the_same_function(pair):
    we, mod = pair
    for turn in (1, 2, 17):
        assert we.delegation_id(JOB_ID, turn) == \
            mod._webui_delegation_id({"job_id": JOB_ID, "turn": turn})
    assert we.delegation_id(JOB_ID, 1) == \
        mod._webui_delegation_id({"job_id": JOB_ID, "turn": "1"})
    assert we.delegation_id(JOB_ID, 1) == f"pi-bridge-{JOB_ID}-t1"


# ---------------------------------------------------------------------------
# the event shape
# ---------------------------------------------------------------------------

EVENT_KEYS = {"type", "delegation_id", "session_key", "origin_ui_session_id",
              "origin_session_id", "parent_session_id", "goal", "context",
              "toolsets", "role", "model", "is_batch", "status", "summary",
              "error", "dispatched_at", "completed_at"}
# documented divergences, asserted separately below
TIMESTAMP_KEYS = {"completed_at"}


@pytest.mark.parametrize("status,error", [("completed", None),
                                          ("failed", "pi exited 3"),
                                          ("failed", None)])
def test_build_event_fields_agree(pair, utc, status, error):
    we, mod = pair
    written = req_fixture(we, turn=2, status=status, error=error)
    req = we.read_request(state.job_dir(JOB_ID))
    assert req["status"] == written["status"]

    a = we.build_event(req, task_preview="do the thing")
    b = mod._webui_build_event(req)

    assert set(a) == set(b) == EVENT_KEYS
    for key in EVENT_KEYS - TIMESTAMP_KEYS:
        assert a[key] == b[key], key
    assert a["delegation_id"] == b["delegation_id"]
    assert a["summary"] == b["summary"]
    assert a["error"] == b["error"]
    assert a["status"] == b["status"] == status
    # completion time is "now" in both (same call window)
    assert abs(a["completed_at"] - b["completed_at"]) < 5
    # goal is byte-identical: both read it from the spool request
    assert a["goal"] == b["goal"] == f"Pi job {JOB_ID} (turn 2): do it"


def test_build_event_timestamps_agree_on_any_host(pair, utc):
    """created_at is written as a UTC instant; both implementations must read
    it back as the same epoch regardless of the host timezone."""
    import time as _time
    we, mod = pair
    req_fixture(we)
    req = we.read_request(state.job_dir(JOB_ID))
    a, b = we.build_event(req), mod._webui_build_event(req)
    assert a["dispatched_at"] == pytest.approx(b["dispatched_at"], abs=1.0)
    # and the epoch is really the UTC reading of created_at, not local time
    expected = _time.strptime(req["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    import calendar
    assert a["dispatched_at"] == pytest.approx(calendar.timegm(expected),
                                               abs=1.0)


# ---------------------------------------------------------------------------
# the file protocol, driven through both implementations
# ---------------------------------------------------------------------------

def test_write_by_bridge_is_claimed_and_released_by_the_plugin(pair):
    we, mod = pair
    req_fixture(we, text="from the runner")

    scanned = mod._webui_scan()
    assert [r["job_id"] for r in scanned] == [JOB_ID]
    claimed = mod._webui_claim(scanned[0])
    assert claimed is not None
    inflight = Path(claimed["_inflight_path"])
    assert inflight.name == f"{we.REQUEST_NAME}.inflight.{os.getpid()}"
    assert not (state.job_dir(JOB_ID) / we.REQUEST_NAME).exists()
    # the bridge's own claim must now lose the race
    assert we.claim({"_dir": str(state.job_dir(JOB_ID))}) is None
    # the bridge must not see a claimed request as actionable either
    assert we.iter_requests() == []

    mod._webui_release(claimed, "injected")
    got = we.read_request(state.job_dir(JOB_ID))
    assert got["state"] == "injected" and got["text"] == "from the runner"
    assert got["attempts"] == 0
    assert [r["job_id"] for r in we.iter_requests()] == [JOB_ID]
    assert list(state.job_dir(JOB_ID).glob(we.REQUEST_NAME + ".inflight.*")) \
        == []


def test_release_by_bridge_is_visible_to_the_plugin(pair):
    we, mod = pair
    req_fixture(we, text="bridge-side transitions")
    req = we.read_request(state.job_dir(JOB_ID))
    claimed = we.claim(req)
    we.release(claimed, state_value="injected")

    scanned = mod._webui_scan()
    assert [r["job_id"] for r in scanned] == [JOB_ID]
    assert scanned[0]["state"] == "injected"

    claimed = we.claim(we.read_request(state.job_dir(JOB_ID)))
    we.release(claimed, state_value="delivered", reason="ok")
    assert mod._webui_scan() == []             # terminal for both views
    assert we.iter_requests() == []
    on_disk = json.loads((state.job_dir(JOB_ID) / we.REQUEST_NAME)
                         .read_text("utf-8"))
    assert on_disk["state"] == "delivered" and on_disk["reason"] == "ok"
    assert not [k for k in on_disk if k.startswith("_")]


def test_supersede_is_invisible_as_a_second_request(pair):
    we, mod = pair
    req_fixture(we, turn=1, text="one")
    req_fixture(we, turn=2, text="two")
    assert [r["turn"] for r in mod._webui_scan()] == [2]
    assert [r["turn"] for r in we.iter_requests()] == [2]


def test_both_views_list_the_same_requests_in_the_same_order(pair):
    """Newest job first, identical visibility (a different order would only
    matter for fairness, a different SET would mean lost deliveries)."""
    we, mod = pair
    req_fixture(we, text="first job")
    we.write_request(JOB_ID_2, ui_session_id=SID, turn=1, text="second job")
    bridge_view = [(r["job_id"], r["state"]) for r in we.iter_requests()]
    plugin_view = [(r["job_id"], r["state"]) for r in mod._webui_scan()]
    assert bridge_view == plugin_view
    assert [j for j, _ in bridge_view] == [JOB_ID_2, JOB_ID]


# ---------------------------------------------------------------------------
# the ledger row the injection writes
# ---------------------------------------------------------------------------

def test_inject_uses_the_shared_event_and_the_shared_ledger_row(pair, utc):
    we, mod = pair
    req_fixture(we, turn=3, text="outcome")
    req = we.read_request(state.job_dir(JOB_ID))

    captured = {}

    class Cur:
        def fetchone(self):
            return None

    class Conn:
        def execute(self, sql, params=()):
            captured["sql"] = " ".join(sql.split())
            captured["params"] = tuple(params)
            return Cur()

    @contextlib.contextmanager
    def tx():
        yield Conn()

    class Reg:
        def __init__(self):
            self.completion_queue = queue.Queue()

    reg = Reg()
    evt = mod._webui_inject((contextlib.nullcontext(), tx, reg), req)
    row = we.ledger_row(we.build_event(req, task_preview=""))

    assert captured["sql"].startswith("INSERT OR IGNORE INTO async_delegations")
    assert "delivery_state" in captured["sql"] and "'pending'" in \
        captured["sql"], "the durable row must start out deliverable"
    params = captured["params"]
    assert len(params) == len(row) == 9
    assert params[:5] == row[:5]
    assert params[5] == pytest.approx(row[5], abs=1.0)       # dispatched_at
    assert json.loads(params[8])["delegation_id"] == row[0]
    assert reg.completion_queue.qsize() == 1
    assert evt["delegation_id"] == row[0]
    assert evt["origin_ui_session_id"] == SID


def test_release_drops_a_stale_reason_identically_in_both(pair):
    """release(state, reason=None) must NOT preserve a previous reason --
    both implementations strip the key, so 'reason' always describes the
    current state, never an older failed transition."""
    we, mod = pair
    req_fixture(we)
    path = state.job_dir(JOB_ID) / we.REQUEST_NAME
    req = we.read_request(state.job_dir(JOB_ID))
    req["reason"] = "stale-noise"
    state.write_text_atomic(path, json.dumps(req, ensure_ascii=False))

    out = we.release(we.claim(we.read_request(state.job_dir(JOB_ID))),
                     "pending")
    assert "reason" not in out
    assert "reason" not in we.read_request(state.job_dir(JOB_ID))

    # same file, same transition, plugin mirror: no reason either
    scanned = mod._webui_scan()
    assert [r["job_id"] for r in scanned] == [JOB_ID]
    out2 = mod._webui_release(mod._webui_claim(scanned[0]), "pending")
    assert "reason" not in out2
    assert "reason" not in we.read_request(state.job_dir(JOB_ID))
