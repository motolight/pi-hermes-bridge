"""v0.4.1 plugin webui completion poller: injection, acks and the host gate.

The poller lives inside the Hermes process (plugin/__init__.py).  Its only
external dependencies are Hermes internals -- `_DB_LOCK`, `_transaction` and
`process_registry.completion_queue` -- which is exactly the seam this test
drives: a fake ledger + a real queue.Queue, a temp PI_BRIDGE_HOME spool, and
`_webui_poll_once()` called directly (no threads, no Hermes, no subprocess).

Covered behaviours (docs/WEBUI_COMPLETION_EVENT_SPEC.md):
* inject once per (job, turn) with a stable delegation_id, durable ledger row
  and the canonical spool moved to "injected";
* two jobs of one session are two independent events;
* a session whose ledger row is claimed elsewhere gains no extra event;
* ack transitions: delivered -> "delivered" + job delivery ok,
  dropped -> "abandoned" + delivery not ok;
* the host gate: no tui_gateway.server and no self-owned lease -> nothing is
  injected and the request stays pending;
* re-injection bumps `attempts` and the attempt budget ends the request.
"""
import contextlib
import importlib.util
import json
import os
import queue
import sys
from pathlib import Path

import pytest

from conftest import REPO
from pi_bridge import state, webui_event

JID_A = "pb-20260101T000000Z-aaaaaa"
JID_B = "pb-20260101T000001Z-bbbbbb"
SID = "ee58f3039666"
NAME = "webui_delivery.json"


def _load_plugin(name="pi_worker_plugin_poller_test"):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "plugin" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# the fake Hermes internals
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    """Only the two statements _webui_poll_once actually issues."""

    def __init__(self, ledger):
        self.ledger = ledger

    def execute(self, sql, params=()):
        head = " ".join(sql.split()).upper()
        if head.startswith("SELECT"):
            row = self.ledger.get(str(params[0]))
            if row is None:
                return FakeCursor(None)
            return FakeCursor((row["delivery_state"], row["delivery_claim"]))
        if head.startswith("INSERT"):
            did = str(params[0])
            if did not in self.ledger:          # INSERT OR IGNORE semantics
                self.ledger[did] = {
                    "delivery_state": "pending", "delivery_claim": None,
                    "state": params[4], "event_json": params[8],
                    "params": tuple(params)}
            return FakeCursor(None)
        raise AssertionError(f"unexpected SQL in the poller: {sql!r}")


class FakeRegistry:
    def __init__(self):
        self.completion_queue = queue.Queue()


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """Plugin module + fake Hermes internals + an empty spool."""
    home = tmp_path / "bridge-home"
    (home / "jobs").mkdir(parents=True)
    monkeypatch.setenv("PI_BRIDGE_HOME", str(home))
    # deterministic persistent-identity inputs: no webui role from the
    # ambient environment, no attempt-cap override
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_BRIDGE_MAX_INJECTION_ATTEMPTS", raising=False)
    # the real ~/.hermes must never be consulted: an empty hermes home, no lease
    fake_home = tmp_path / "fake-home"
    (fake_home / ".hermes" / "runtime").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert "tui_gateway.server" not in sys.modules

    mod = _load_plugin()
    real_may_host = mod._webui_may_host
    ledger = {}
    registry = FakeRegistry()

    @contextlib.contextmanager
    def fake_transaction():
        yield FakeConn(ledger)

    def fake_hermes():
        return (contextlib.nullcontext(), fake_transaction, registry)

    monkeypatch.setattr(mod, "_webui_hermes", fake_hermes)
    monkeypatch.setattr(mod, "_webui_may_host", lambda sid: True)
    return type("H", (), {"mod": mod, "home": home, "ledger": ledger,
                          "registry": registry, "tmp": tmp_path,
                          "hermes_home": fake_home / ".hermes",
                          "real_may_host": real_may_host,
                          "fake_hermes": fake_hermes,
                          "monkeypatch": monkeypatch})


def make_job(h, job_id=JID_A, sid=SID, turn=1, text="Pi-задача ...\n\nok"):
    jd = h.home / "jobs" / job_id
    jd.mkdir(parents=True)
    state.write_text_atomic(
        jd / "job.json",
        json.dumps({"job_id": job_id, "status": "completed", "task": "t",
                    "turns": [{"n": turn}], "final_result": "ok",
                    "delivery": {}}))
    webui_event.write_request(job_id, ui_session_id=sid, turn=turn, text=text)
    return jd


def on_disk(h, job_id=JID_A):
    return webui_event.read_request(h.home / "jobs" / job_id)


def inflights(h, job_id=JID_A):
    return list((h.home / "jobs" / job_id).glob(NAME + ".inflight.*"))


def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def write_lease(h, pid, sid=SID):
    """The shared Hermes lease registry (~/.hermes/runtime/...)."""
    (h.hermes_home / "runtime" / "active_sessions.json").write_text(
        json.dumps({"entries": [{"session_id": sid, "pid": pid,
                                 "surface": "webui"}]}), "utf-8")


# ---------------------------------------------------------------------------
# injection
# ---------------------------------------------------------------------------

def test_pending_request_is_injected_once_when_we_host_the_session(harness):
    h = harness
    make_job(h)

    h.mod._webui_poll_once()

    events = drain(h.registry.completion_queue)
    assert len(events) == 1
    evt = events[0]
    assert evt["type"] == "async_delegation"
    assert evt["delegation_id"] == f"pi-bridge-{JID_A}-t1"
    assert evt["origin_ui_session_id"] == SID
    assert evt["session_key"] == SID
    assert evt["summary"] == "Pi-задача ...\n\nok"
    assert evt["status"] == "completed" and evt["error"] is None
    assert evt["is_batch"] is False

    # the durable ledger row that gives at-most-once acceptance + restart replay
    row = h.ledger[f"pi-bridge-{JID_A}-t1"]
    assert row["state"] == "completed"
    assert row["delivery_state"] == "pending"
    assert row["delivery_claim"] is None
    assert row["params"][2] == SID
    assert json.loads(row["event_json"])["delegation_id"] == row["params"][0]

    # the spool moved on, and no inflight garbage is left behind
    req = on_disk(h)
    assert req["state"] == "injected"
    assert req["attempts"] == 1
    assert inflights(h) == []


def test_two_jobs_of_one_session_are_two_independent_events(harness):
    h = harness
    make_job(h, JID_A, text="first outcome")
    make_job(h, JID_B, text="second outcome")

    h.mod._webui_poll_once()

    events = drain(h.registry.completion_queue)
    assert len(events) == 2
    ids = {e["delegation_id"] for e in events}
    assert ids == {f"pi-bridge-{JID_A}-t1", f"pi-bridge-{JID_B}-t1"}
    assert len(ids) == 2
    assert {e["summary"] for e in events} == {"first outcome",
                                              "second outcome"}
    # both are still routed to the SAME session
    assert all(e["origin_ui_session_id"] == SID for e in events)
    assert on_disk(h, JID_A)["state"] == "injected"
    assert on_disk(h, JID_B)["state"] == "injected"


def test_busy_consumer_claim_suppresses_reinjection(harness):
    """Hermes' notification poller holds the delivery claim (the session is
    busy): the bridge must not queue a second event for the same turn, and
    the request must stay VISIBLE (re-published to the canonical file) so
    the ack is still observed when Hermes finishes."""
    h = harness
    make_job(h)
    h.mod._webui_poll_once()
    assert len(drain(h.registry.completion_queue)) == 1
    h.ledger[f"pi-bridge-{JID_A}-t1"]["delivery_claim"] = "hermes-4242"

    h.mod._webui_poll_once()

    # the point of the test: no second event for a turn already in flight
    assert drain(h.registry.completion_queue) == []
    assert h.ledger[f"pi-bridge-{JID_A}-t1"]["delivery_state"] == "pending"
    # the request is re-published, not stranded in the inflight copy
    assert on_disk(h)["state"] == "injected"
    assert inflights(h) == []
    h.ledger[f"pi-bridge-{JID_A}-t1"]["delivery_state"] = "delivered"
    h.ledger[f"pi-bridge-{JID_A}-t1"]["delivery_claim"] = None
    h.mod._webui_poll_once()
    assert on_disk(h)["state"] == "delivered"
    job = json.loads((h.home / "jobs" / JID_A / "job.json").read_text("utf-8"))
    assert job["delivery"]["ok"] is True and job["delivery"]["reason"] == "ok"


# ---------------------------------------------------------------------------
# acks
# ---------------------------------------------------------------------------

def test_ledger_delivered_acks_the_request_and_the_job(harness):
    h = harness
    make_job(h)
    h.mod._webui_poll_once()
    drain(h.registry.completion_queue)
    h.ledger[f"pi-bridge-{JID_A}-t1"]["delivery_state"] = "delivered"

    h.mod._webui_poll_once()

    req = on_disk(h)
    assert req["state"] == "delivered" and req["reason"] == "ok"
    assert inflights(h) == []
    job = json.loads((h.home / "jobs" / JID_A / "job.json").read_text("utf-8"))
    d = job["delivery"]
    assert (d["attempted"], d["ok"], d["kind"], d["reason"],
            d["channel"]) == (True, True, "webui-event", "ok", f"webui:{SID}")
    assert job["status"] == "completed"        # delivery never changes status
    # a delivered request is terminal: nothing more is ever queued
    h.mod._webui_poll_once()
    assert drain(h.registry.completion_queue) == []
    assert on_disk(h)["state"] == "delivered"


def test_ledger_dropped_abandons_the_request(harness):
    h = harness
    make_job(h)
    h.mod._webui_poll_once()
    drain(h.registry.completion_queue)
    h.ledger[f"pi-bridge-{JID_A}-t1"]["delivery_state"] = "dropped"

    h.mod._webui_poll_once()

    req = on_disk(h)
    assert req["state"] == "abandoned"
    assert req["reason"] == "native-delivery-exhausted"
    job = json.loads((h.home / "jobs" / JID_A / "job.json").read_text("utf-8"))
    assert (job["delivery"]["attempted"], job["delivery"]["ok"],
            job["delivery"]["reason"]) == (True, False,
                                           "native-delivery-exhausted")
    assert job["status"] == "completed"
    h.mod._webui_poll_once()
    assert drain(h.registry.completion_queue) == []


def test_missing_ledger_row_reinjects_the_same_delegation_id(harness):
    h = harness
    make_job(h)
    h.mod._webui_poll_once()
    drain(h.registry.completion_queue)
    h.ledger.clear()                           # the row vanished (fresh DB)

    h.mod._webui_poll_once()

    events = drain(h.registry.completion_queue)
    assert [e["delegation_id"] for e in events] == [f"pi-bridge-{JID_A}-t1"]
    assert on_disk(h)["state"] == "injected"


# ---------------------------------------------------------------------------
# the host gate
# ---------------------------------------------------------------------------

def test_a_process_that_does_not_host_the_session_injects_nothing(harness,
                                                                 monkeypatch):
    """The REAL gate, unstubbed: no tui_gateway.server in this process and the
    lease belongs to another pid -> the request stays pending, untouched."""
    h = harness
    make_job(h)
    monkeypatch.setattr(h.mod, "_webui_may_host", h.real_may_host)
    write_lease(h, os.getpid() + 1)
    assert h.mod._webui_may_host(SID) is False

    h.mod._webui_poll_once()

    assert drain(h.registry.completion_queue) == []
    assert h.ledger == {}
    assert on_disk(h)["state"] == "pending"
    assert inflights(h) == []


def test_gate_false_keeps_the_request_pending(harness, monkeypatch):
    h = harness
    make_job(h)
    monkeypatch.setattr(h.mod, "_webui_may_host", lambda sid: False)

    h.mod._webui_poll_once()

    assert drain(h.registry.completion_queue) == []
    assert h.ledger == {}
    assert on_disk(h)["state"] == "pending"     # harmlessly queued
    assert inflights(h) == []


def test_gate_true_via_lease_owned_by_this_pid(harness, monkeypatch):
    """The real gate: a CLI that legitimately owns the session may complete a
    queued delivery (lease pid == os.getpid())."""
    h = harness
    make_job(h)
    monkeypatch.setattr(h.mod, "_webui_may_host", h.real_may_host)
    write_lease(h, os.getpid())
    assert h.mod._webui_may_host(SID) is True
    h.mod._webui_poll_once()
    assert len(drain(h.registry.completion_queue)) == 1
    assert on_disk(h)["state"] == "injected"


# ---------------------------------------------------------------------------
# the injection budget
# ---------------------------------------------------------------------------

def test_unclaimed_pending_completion_is_reinjected_with_attempts(harness):
    h = harness
    make_job(h)
    h.mod._webui_poll_once()
    assert on_disk(h)["attempts"] == 1
    drain(h.registry.completion_queue)

    h.mod._webui_poll_once()                    # pending + unclaimed -> retry
    assert on_disk(h)["attempts"] == 2
    assert [e["delegation_id"] for e in drain(h.registry.completion_queue)] \
        == [f"pi-bridge-{JID_A}-t1"]
    assert on_disk(h)["state"] == "injected"


def test_injection_budget_exhaustion_abandons_an_injected_request(harness):
    h = harness
    jd = make_job(h)
    h.mod._webui_poll_once()                    # creates the ledger row
    drain(h.registry.completion_queue)
    req = dict(on_disk(h), state="injected",
               attempts=int(h.mod._WEBUI_MAX_ATTEMPTS))
    state.write_text_atomic(jd / NAME, json.dumps(req))

    h.mod._webui_poll_once()

    assert on_disk(h)["state"] == "abandoned"
    assert on_disk(h)["reason"] == "injection-attempts-exhausted"
    job = json.loads((jd / "job.json").read_text("utf-8"))
    assert (job["delivery"]["attempted"], job["delivery"]["ok"],
            job["delivery"]["reason"]) == (True, False,
                                           "injection-attempts-exhausted")
    assert drain(h.registry.completion_queue) == []


def test_injection_budget_exhaustion_abandons_a_pending_request(harness):
    h = harness
    jd = make_job(h)
    req = dict(on_disk(h), state="pending",
               attempts=int(h.mod._WEBUI_MAX_ATTEMPTS))
    state.write_text_atomic(jd / NAME, json.dumps(req))

    h.mod._webui_poll_once()

    assert on_disk(h)["state"] == "abandoned"
    assert on_disk(h)["reason"] == "injection-attempts-exhausted"
    assert drain(h.registry.completion_queue) == []


# ---------------------------------------------------------------------------
# robustness of the tick itself
# ---------------------------------------------------------------------------

def test_a_failing_request_returns_to_pending_and_the_tick_survives(harness,
                                                                    monkeypatch):
    h = harness
    make_job(h)
    make_job(h, JID_B)
    boom = {"fail": True}
    real = h.mod._webui_inject

    def flaky(hermes, req):
        if req["job_id"] == JID_A and boom["fail"]:
            boom["fail"] = False
            raise RuntimeError("ledger exploded")
        return real(hermes, req)

    monkeypatch.setattr(h.mod, "_webui_inject", flaky)

    h.mod._webui_poll_once()                    # must not raise, must not die

    assert on_disk(h, JID_A)["state"] == "pending"   # retried on the next tick
    assert on_disk(h, JID_B)["state"] == "injected"  # the sibling still landed
    assert [e["delegation_id"]
            for e in drain(h.registry.completion_queue)] == \
        [f"pi-bridge-{JID_B}-t1"]
    assert inflights(h, JID_A) == []


def test_no_hermes_internals_marks_unsupported_and_recovers(harness,
                                                            monkeypatch):
    """A process without the native pipeline (no tools.async_delegation)
    marks the request 'unsupported' (visible via pi_status) and never
    half-delivers; when the internals become importable the same request is
    injected normally -- the state is a marker, not a dead end."""
    h = harness
    make_job(h)
    real_hermes = h.mod._webui_hermes
    monkeypatch.setattr(h.mod, "_webui_hermes", lambda: None)
    h.mod._webui_poll_once()
    assert drain(h.registry.completion_queue) == []
    req = on_disk(h)
    assert req["state"] == "unsupported"
    assert req["reason"] == "hermes-internal-unavailable"
    monkeypatch.setattr(h.mod, "_webui_hermes", real_hermes)
    h.mod._webui_poll_once()
    assert on_disk(h)["state"] == "injected"
    assert len(drain(h.registry.completion_queue)) == 1


# ---------------------------------------------------------------------------
# the gateway-shaped process must never inject
# ---------------------------------------------------------------------------

def test_gateway_shaped_process_injects_nothing(harness, monkeypatch):
    """The gateway imports tui_gateway.server itself (hosted-room worker).
    Its delegation watcher terminally drops completions whose parent it
    cannot resolve, so a process with BOTH modules imported must stay out:
    the request remains pending for the real backend."""
    h = harness
    make_job(h)
    monkeypatch.setattr(h.mod, "_webui_may_host", h.real_may_host)
    monkeypatch.setitem(sys.modules, "gateway.run",
                        type(sys)("gateway.run"))
    monkeypatch.setitem(sys.modules, "tui_gateway.server",
                        type(sys)("tui_gateway.server"))
    h.mod._webui_poll_once()
    assert drain(h.registry.completion_queue) == []
    assert on_disk(h)["state"] == "pending"


@pytest.mark.parametrize("marker", ["api.background_process",
                                    "tui_gateway.server"])
def test_backend_shaped_process_does_inject(harness, monkeypatch, marker):
    """Same job, but a backend marker module is loaded: the gate opens.
    api.background_process is the Hermes WebUI drain thread (production
    consumer); tui_gateway.server covers the tui/desktop poller."""
    h = harness
    make_job(h)
    monkeypatch.setattr(h.mod, "_webui_may_host", h.real_may_host)
    monkeypatch.setitem(sys.modules, marker, type(sys)(marker))
    h.mod._webui_poll_once()
    assert len(drain(h.registry.completion_queue)) == 1
    assert on_disk(h)["state"] == "injected"


def test_hermes_home_env_lease_claims_host_for_this_pid(harness, monkeypatch):
    """A CLI that legitimately owns the session's lease (profile deployment,
    $HERMES_HOME) can complete a queued delivery in-process."""
    import os as _os
    h = harness
    make_job(h)
    hh = h.tmp / "profile-hermes"
    (hh / "runtime").mkdir(parents=True)
    (hh / "runtime" / "active_sessions.json").write_text(json.dumps(
        {"entries": [{"session_id": SID, "pid": _os.getpid(),
                      "surface": "cli"}]}), "utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hh))
    monkeypatch.setattr(h.mod, "_webui_may_host", h.real_may_host)
    h.mod._webui_poll_once()
    assert on_disk(h)["state"] == "injected"
    assert len(drain(h.registry.completion_queue)) == 1


# ---------------------------------------------------------------------------
# v0.4.2 process-role gate (incident pb-20261019T082242Z-8d9299)
# ---------------------------------------------------------------------------

PYTEST_ARGV = [str(Path(sys.executable)), "-m", "pytest", "tests"]


def test_process_role_table(harness):
    """Persistent identity only -- argv, environment, __main__ file."""
    role = harness.mod._webui_process_role
    gw = "gateway"
    assert role([sys.executable, "-m", "hermes_cli.main", "gateway", "run"],
                {}, "/venv/bin/python") == gw
    assert role(["/h/venv/bin/hermes", "gateway", "run"],
                {}, "/h/venv/bin/hermes") == gw
    assert role([sys.executable, "-m", "gateway.run"], {}, "") == gw
    assert role([sys.executable], {}, "/opt/hermes/gateway/run.py") == gw
    # the gateway check comes FIRST: even webui-looking env cannot launder
    # an argv-identified gateway runner
    assert role([sys.executable, "-m", "hermes_cli.main", "gateway", "run"],
                {"HERMES_WEBUI_STATE_DIR": "/s"}, "") == gw
    webui = "webui"
    assert role(PYTEST_ARGV, {"HERMES_WEBUI_STATE_DIR": "/s"},
                "/venv/bin/pytest") == webui
    assert role(PYTEST_ARGV, {"HERMES_WEBUI_AGENT_DIR": "/a"},
                "/venv/bin/pytest") == webui
    assert role([], {}, "/srv/hermes-webui/server.py") == webui
    assert role([], {}, "/srv/hermes-webui/bootstrap.py") == webui
    tui = "tui_backend"
    assert role([sys.executable, "-m", "tui_gateway.entry"], {}, "") == tui
    assert role([sys.executable, "-m", "tui_gateway"], {}, "") == tui
    assert role([], {}, "/app/tui_gateway/entry.py") == tui
    unknown = "unknown"
    assert role(PYTEST_ARGV, {}, str(Path(sys.executable) /
                                     "pytest")) == unknown
    assert role(PYTEST_ARGV, {"HERMES_WEBUI_STATE_DIR": ""},
                "/venv/bin/pytest") == unknown      # empty env does not count
    # hermes_cli.main without a 'gateway' subcommand is not the runner
    assert role([sys.executable, "-m", "hermes_cli.main", "chat"], {},
                "") == unknown
    # 'hermesx' is not the 'hermes' console script
    assert role(["/h/bin/hermesx", "gateway", "run"], {}, "") == unknown


def test_webui_host_survives_the_late_gateway_run_import(harness, monkeypatch):
    """THE production regression, end to end: a clean webui host (webui env
    markers, no webui-shaped argv) completes one delivery, then lazily
    imports gateway.run (agent/relay_runtime.py via compression) -- the
    second completion must STILL be injected, not poison the poller."""
    h = harness
    monkeypatch.setattr(h.mod, "_webui_may_host", h.real_may_host)
    monkeypatch.setattr(sys, "argv", list(PYTEST_ARGV))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(h.tmp / "webui-state"))

    make_job(h, JID_A, text="first outcome")
    h.mod._webui_poll_once()
    assert len(drain(h.registry.completion_queue)) == 1
    assert on_disk(h, JID_A)["state"] == "injected"

    # the exact poisoning event, AFTER the first completion:
    monkeypatch.setitem(sys.modules, "gateway.run", type(sys)("gateway.run"))
    assert h.mod._webui_host_decision(SID) == (True, "webui-positive")

    make_job(h, JID_B, text="second outcome")
    h.mod._webui_poll_once()
    # JID_A's ledger row is still pending/unclaimed, so its event is
    # re-queued too -- the point is that JID_B's NEW completion is there
    # even though gateway.run is now loaded in this webui host.
    assert {e["delegation_id"] for e in drain(h.registry.completion_queue)} \
        == {f"pi-bridge-{JID_A}-t1", f"pi-bridge-{JID_B}-t1"}
    assert on_disk(h, JID_B)["state"] == "injected"


def test_webui_role_purely_from_env_beats_loaded_gateway_run(harness,
                                                              monkeypatch):
    h = harness
    make_job(h)
    monkeypatch.setattr(h.mod, "_webui_may_host", h.real_may_host)
    monkeypatch.setattr(sys, "argv", list(PYTEST_ARGV))   # argv says pytest
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", "/srv/webui-state")
    monkeypatch.setitem(sys.modules, "gateway.run", type(sys)("gateway.run"))
    monkeypatch.setitem(sys.modules, "tui_gateway.server",
                        type(sys)("tui_gateway.server"))
    assert h.mod._webui_may_host(SID) is True
    h.mod._webui_poll_once()
    assert len(drain(h.registry.completion_queue)) == 1
    assert on_disk(h)["state"] == "injected"


GATEWAY_ARGVS = [
    ["/h/venv/bin/python", "-m", "hermes_cli.main", "gateway", "run"],
    ["/h/venv/bin/hermes", "gateway", "run"],           # console script
    ["/h/venv/bin/python", "-m", "gateway.run"],
]


@pytest.mark.parametrize("argv", GATEWAY_ARGVS)
@pytest.mark.parametrize("mods", [
    ("gateway.run", "tui_gateway.server"),   # fully-imported gateway
    ("tui_gateway.server",),                 # BEFORE the lazy gateway.run
])
def test_gateway_runner_never_injects(harness, monkeypatch, argv, mods):
    """A real gateway runner must stay out of injection regardless of which
    modules it has loaded -- the argv-based veto holds even when
    gateway.run is not yet imported (the mirror of the poisoning bug: the
    watcher terminally DROPS completions whose parent it cannot resolve).
    The request stays pending for the real backend."""
    h = harness
    make_job(h)
    monkeypatch.setattr(h.mod, "_webui_may_host", h.real_may_host)
    monkeypatch.setattr(sys, "argv", list(argv))
    for m in mods:
        monkeypatch.setitem(sys.modules, m, type(sys)(m))
    write_lease(h, os.getpid())     # even a self-held lease must not unlock
    assert h.mod._webui_host_decision(SID) == (False, "gateway-runner")
    h.mod._webui_poll_once()
    assert drain(h.registry.completion_queue) == []
    assert h.ledger == {}
    assert on_disk(h)["state"] == "pending"


def test_gateway_argv_overrides_the_backend_marker_and_webui_modules(
        harness, monkeypatch):
    """Rule order: the persistent gateway identity vetoes before any
    positive marker is consulted (a real gateway imports
    tui_gateway.server itself)."""
    h = harness
    monkeypatch.setattr(h.mod, "_webui_may_host", h.real_may_host)
    monkeypatch.setattr(sys, "argv",
                        [sys.executable, "-m", "hermes_cli.main",
                         "gateway", "run"])
    monkeypatch.setitem(sys.modules, "tui_gateway.server",
                        type(sys)("tui_gateway.server"))
    monkeypatch.setitem(sys.modules, "api.background_process",
                        type(sys)("api.background_process"))
    assert h.mod._webui_host_decision(SID) == (False, "gateway-runner")


def test_api_background_process_alone_is_webui_positive(harness, monkeypatch):
    """api.background_process is a hermes-webui-only module: its presence
    is a POSITIVE identity that a late gateway.run import cannot veto."""
    h = harness
    monkeypatch.setattr(h.mod, "_webui_may_host", h.real_may_host)
    monkeypatch.setattr(sys, "argv", list(PYTEST_ARGV))
    monkeypatch.setitem(sys.modules, "api.background_process",
                        type(sys)("api.background_process"))
    monkeypatch.setitem(sys.modules, "gateway.run", type(sys)("gateway.run"))
    assert h.mod._webui_host_decision(SID) == (True, "webui-positive")


def test_tui_backend_role_hosts_and_marker_still_allowed(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(h.mod, "_webui_may_host", h.real_may_host)
    monkeypatch.setattr(sys, "argv",
                        [sys.executable, "-m", "tui_gateway.entry"])
    assert h.mod._webui_host_decision(SID) == (True, "tui-backend")
    # unknown role + tui_gateway.server alone: the legacy marker path keeps
    # working (existing behaviour preserved)
    monkeypatch.setattr(sys, "argv", list(PYTEST_ARGV))
    monkeypatch.setitem(sys.modules, "tui_gateway.server",
                        type(sys)("tui_gateway.server"))
    assert h.mod._webui_host_decision(SID) == (True, "backend-marker")


def test_unknown_role_loaded_gateway_run_still_vetoes(harness, monkeypatch):
    """v0.4.1 behaviour preserved for argv-unknown processes: a loaded
    gateway.run vetoes even a self-held lease."""
    h = harness
    monkeypatch.setattr(h.mod, "_webui_may_host", h.real_may_host)
    monkeypatch.setattr(sys, "argv", list(PYTEST_ARGV))
    monkeypatch.setitem(sys.modules, "gateway.run", type(sys)("gateway.run"))
    write_lease(h, os.getpid())
    assert h.mod._webui_host_decision(SID) == (False, "gateway-run-loaded")


# ---------------------------------------------------------------------------
# v0.4.2 durable attempt budget: failed attempts are NOT free
# ---------------------------------------------------------------------------

def test_transient_inject_failure_bumps_durable_attempts_and_retries(
        harness, monkeypatch):
    """A raised injection is still a REAL attempt: attempts+1 must be
    persisted, the request returns to pending, and the retry succeeds all
    the way to delivered."""
    h = harness
    jd = make_job(h)
    boom = {"fail": True}
    real = h.mod._webui_inject

    def flaky(hermes, req):
        if boom["fail"]:
            boom["fail"] = False
            raise RuntimeError("transient ledger failure")
        return real(hermes, req)

    monkeypatch.setattr(h.mod, "_webui_inject", flaky)

    h.mod._webui_poll_once()
    req = on_disk(h)
    assert req["state"] == "pending" and req["attempts"] == 1

    h.mod._webui_poll_once()
    assert on_disk(h)["state"] == "injected" and on_disk(h)["attempts"] == 2
    assert len(drain(h.registry.completion_queue)) == 1

    h.ledger[f"pi-bridge-{JID_A}-t1"]["delivery_state"] = "delivered"
    h.mod._webui_poll_once()
    assert on_disk(h)["state"] == "delivered"
    job = json.loads((jd / "job.json").read_text("utf-8"))
    assert job["delivery"]["ok"] is True


def test_vanished_ledger_row_reinjection_bumps_attempts(harness):
    h = harness
    make_job(h)
    h.mod._webui_poll_once()
    assert on_disk(h)["attempts"] == 1
    drain(h.registry.completion_queue)
    h.ledger.clear()                               # the row vanished

    h.mod._webui_poll_once()

    assert on_disk(h)["state"] == "injected"
    assert on_disk(h)["attempts"] == 2             # the re-inject was real
    assert len(drain(h.registry.completion_queue)) == 1


def test_persistent_inject_failures_exhaust_env_cap_and_survive_restart(
        harness, monkeypatch):
    """Repeated raising attempts must reach 'abandoned' (job acked
    ok=false), the cap comes from PI_BRIDGE_MAX_INJECTION_ATTEMPTS, and a
    process restart (brand-new plugin module instance over the same spool)
    must NOT reset the durable counter."""
    h = harness
    jd = make_job(h)
    monkeypatch.setenv("PI_BRIDGE_MAX_INJECTION_ATTEMPTS", "3")

    def boom(hermes, req):
        raise RuntimeError("persistent ledger failure")

    def wire(mod):
        monkeypatch.setattr(mod, "_webui_hermes", h.fake_hermes)
        monkeypatch.setattr(mod, "_webui_may_host", lambda sid: True)
        monkeypatch.setattr(mod, "_webui_inject", boom)
        assert mod._WEBUI_MAX_ATTEMPTS == 3        # env override at import
        return mod

    mod1 = wire(_load_plugin("pi_worker_poller_cap1"))
    mod1._webui_poll_once()
    assert on_disk(h)["state"] == "pending" and on_disk(h)["attempts"] == 1

    # simulated restart: a fresh module instance, same PI_BRIDGE_HOME
    mod2 = wire(_load_plugin("pi_worker_poller_cap2"))
    mod2._webui_poll_once()
    assert on_disk(h)["attempts"] == 2
    mod2._webui_poll_once()
    assert on_disk(h)["attempts"] == 3
    assert drain(h.registry.completion_queue) == []

    mod2._webui_poll_once()                        # 3 >= cap -> abandoned
    req = on_disk(h)
    assert req["state"] == "abandoned"
    assert req["reason"] == "injection-attempts-exhausted"
    job = json.loads((jd / "job.json").read_text("utf-8"))
    assert (job["delivery"]["attempted"], job["delivery"]["ok"],
            job["delivery"]["reason"]) == (True, False,
                                           "injection-attempts-exhausted")
    assert drain(h.registry.completion_queue) == []


def test_max_attempts_env_garbage_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("PI_BRIDGE_MAX_INJECTION_ATTEMPTS", "not-an-int")
    mod = _load_plugin("pi_worker_poller_garbage_cap")
    assert mod._WEBUI_MAX_ATTEMPTS == 1200
    monkeypatch.setenv("PI_BRIDGE_MAX_INJECTION_ATTEMPTS", "0")
    mod = _load_plugin("pi_worker_poller_zero_cap")
    assert mod._WEBUI_MAX_ATTEMPTS == 1200
    monkeypatch.delenv("PI_BRIDGE_MAX_INJECTION_ATTEMPTS")
    mod = _load_plugin("pi_worker_poller_default_cap")
    assert mod._WEBUI_MAX_ATTEMPTS == 1200


def test_stuck_spool_is_injected_by_a_fresh_plugin_module(harness, monkeypatch):
    """Backend-restart recovery (the stuck production job): a pending spool
    written BEFORE the plugin module exists is injected by the brand-new
    module instance over the same PI_BRIDGE_HOME."""
    h = harness
    make_job(h)                                    # spool predates the module
    assert on_disk(h)["state"] == "pending"

    mod2 = _load_plugin("pi_worker_poller_recovery")
    monkeypatch.setattr(mod2, "_webui_hermes", h.fake_hermes)
    monkeypatch.setattr(mod2, "_webui_may_host", lambda sid: True)

    mod2._webui_poll_once()
    assert len(drain(h.registry.completion_queue)) == 1
    assert on_disk(h)["state"] == "injected"
    assert on_disk(h)["attempts"] == 1
