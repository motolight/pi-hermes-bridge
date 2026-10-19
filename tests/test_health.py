"""v0.4.2 poller observability: durable health record, rate limits, logs.

Three contracts (see plugin/__init__.py, pi_bridge/health.py, cli.py):
* every poller process keeps $PI_BRIDGE_HOME/health/poller-<pid>.json
  (dir 0700 / file 0600, atomic) up to date: role, gate, tick/scan times,
  pending count, last lifecycle jobs and counters -- and NEVER task text,
  result text, ui_session_id or secrets;
* the record is NOT rewritten every 3 s tick: writes happen on startup, on
  meaningful change and at most on the 60 s heartbeat (measured via the
  plugin's internal write counter);
* logging via the "pi_worker" logger: a start line, gate-reason
  transitions, one line per claim/inject/delivered/abandoned event and
  rate-limited unexpected exceptions -- idle ticks log NOTHING.
Plus the stdlib reader (health.read_records) and the read-only
`pi-bridge health` CLI over the same directory.
"""
import json
import logging
import os
import stat
import subprocess
import sys
import time

from conftest import bridge
from pi_bridge import cli, health
from test_webui_poller import (JID_A, SID, harness, make_job, on_disk)  # noqa: F401

SENTINEL = "SENTINEL-task-text-must-never-reach-health-or-logs"


def health_path(h):
    return h.home / "health" / f"poller-{os.getpid()}.json"


def read_health(h):
    return json.loads(health_path(h).read_text("utf-8"))


def messages(caplog):
    return [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# the plugin-side record
# ---------------------------------------------------------------------------

def test_health_record_tracks_lifecycle_and_leaks_nothing(harness, monkeypatch,
                                                          caplog):
    h = harness
    make_job(h, JID_A, text=SENTINEL)
    monkeypatch.setattr(h.mod, "_webui_may_host", h.real_may_host)
    monkeypatch.setitem(sys.modules, "api.background_process",
                        type(sys)("api.background_process"))

    with caplog.at_level(logging.DEBUG, logger="pi_worker"):
        h.mod._webui_poll_once()                    # -> injected
    p = health_path(h)
    assert p.is_file()
    rec = read_health(h)
    assert rec["pid"] == os.getpid()
    assert rec["protocol"] == "V1.5"
    assert rec["plugin_version"]
    assert rec["role"] == "unknown"                 # this process is pytest
    assert rec["may_host"] is True                  # api.background_process
    assert rec["last_gate_reason"] == "webui-positive"
    assert rec["signals"] == {"gateway_run_loaded": False,
                              "api_background_process_loaded": True,
                              "tui_gateway_server_loaded": False}
    assert rec["started_at"] and rec["last_tick_at"] and rec["last_scan_at"]
    assert rec["last_pending_count"] == 1
    assert rec["last"]["injected_job"] == JID_A
    assert rec["last"]["delivered_job"] is None
    assert rec["last"]["error"] is None
    c = rec["counters"]
    assert (c["ticks"], c["scans"], c["injected"], c["delivered"]) == \
        (1, 1, 1, 0)

    # dir 0700, file 0600
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700

    # CONTENT SAFETY: no task/result text, no ui_session_id anywhere in the
    # durable record or in the captured logs
    raw = p.read_text("utf-8")
    joined = raw + "\n".join(messages(caplog))
    assert SENTINEL not in joined
    assert SID not in joined

    # delivery ack reaches the record too
    h.ledger[f"pi-bridge-{JID_A}-t1"]["delivery_state"] = "delivered"
    h.mod._webui_poll_once()
    rec = read_health(h)
    assert rec["last"]["delivered_job"] == JID_A
    assert rec["counters"]["delivered"] == 1


def test_health_write_rate_limit_and_heartbeat(harness):
    """An idle poller must NOT rewrite the record on every tick; the
    heartbeat and meaningful changes still write."""
    h = harness
    mod = h.mod
    mod._webui_poll_once()                          # startup write
    assert mod._health_write_count == 1
    for _ in range(50):
        mod._webui_poll_once()                      # idle ticks
    assert mod._health_write_count <= 2             # heartbeat slack only

    # a forced-expired heartbeat writes again
    mod._health_last_write_ts -= 61
    mod._webui_poll_once()
    n = mod._health_write_count
    assert n >= 2

    # a meaningful change (a request appears, then an injected job) writes
    # immediately, without waiting for the heartbeat
    make_job(h)
    mod._webui_poll_once()
    assert mod._health_write_count > n

    # the reader sees exactly this process as the only live record
    recs = health.read_records(h.home)
    assert [r["pid"] for r in recs] == [os.getpid()]
    assert recs[0]["counters"]["injected"] == 1


def test_refusing_poller_with_pending_load_still_rate_limits_writes(harness,
                                                                    monkeypatch):
    """The incident's steady state: this process refuses hosting while
    pending requests sit in the spool (gate_rejected/pending_refused_count
    grow every tick).  Growing monotonic counters are NOT a meaningful
    change: the record must still be written at most on the heartbeat."""
    h = harness
    mod = h.mod
    make_job(h)
    monkeypatch.setattr(mod, "_webui_may_host", lambda sid: False)
    mod._webui_poll_once()                        # startup write
    base = mod._health_write_count
    for _ in range(50):
        mod._webui_poll_once()
    assert mod._health_write_count <= base + 1    # heartbeat slack only
    mod._health_last_write_ts -= 61               # expire the heartbeat
    mod._webui_poll_once()                        # the heartbeat flushes it
    rec = read_health(h)
    assert rec["counters"]["gate_rejected"] >= 50  # still counted, not per-tick
    assert rec["pending_refused_count"] >= 50
    assert rec["last_pending_count"] == 1


def test_repeated_identical_injection_errors_do_not_rewrite_every_tick(
        harness, monkeypatch):
    """A permanently failing injector keeps the SAME error type: the error
    timestamp must not turn every 3 s tick into a health write."""
    h = harness
    mod = h.mod
    make_job(h)

    def boom(hermes, req):
        raise RuntimeError("ledger always exploded")

    monkeypatch.setattr(mod, "_webui_inject", boom)
    mod._webui_poll_once()                        # startup write
    mod._webui_poll_once()                        # first error (meaningful)
    base = mod._health_write_count
    for _ in range(50):
        mod._webui_poll_once()
    assert mod._health_write_count <= base + 1    # heartbeat slack only
    rec = read_health(h)
    assert rec["last"]["error"]["type"] == "RuntimeError"
    assert on_disk(h)["state"] == "pending"
    assert on_disk(h)["attempts"] >= 50           # the counter still burns


def test_poller_thread_pins_the_bridge_home(harness, monkeypatch):
    """The daemon thread outlives the fixture env: _start_webui_poller pins
    PI_BRIDGE_HOME at start so later ticks can never scan or write health
    into a different (e.g. real, production) tree after the env changes."""
    h = harness
    mod = h.mod
    other = h.tmp / "other-home"
    (other / "jobs").mkdir(parents=True)
    mod._start_webui_poller()
    make_job(h)                                  # request lands in h.home
    try:
        assert str(mod._bridge_home()) == str(h.home)
        monkeypatch.setenv("PI_BRIDGE_HOME", str(other))
        assert str(mod._bridge_home()) == str(h.home)   # pinned, not env
        mod._webui_poll_once()                          # a "late" tick
        assert (h.home / "health" / f"poller-{os.getpid()}.json").exists()
        assert not (other / "health").exists()
        assert on_disk(h)["state"] == "injected"        # scanned the pinned home
    finally:
        mod._webui_poller_state[1].set()


def test_heartbeat_window_is_60s_not_3s(harness):
    h = harness
    mod = h.mod
    mod._webui_poll_once()
    base = mod._health_write_count
    # many direct write calls inside the heartbeat window (and no meaningful
    # change) must all be suppressed
    for _ in range(400):
        mod._health_write()
    assert mod._health_write_count == base


def test_health_records_injection_errors_and_abandonment(harness, monkeypatch):
    h = harness
    make_job(h)
    monkeypatch.setattr(h.mod, "_WEBUI_MAX_ATTEMPTS", 1)

    def boom(hermes, req):
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr(h.mod, "_webui_inject", boom)
    h.mod._webui_poll_once()                        # attempt 1 fails
    rec = read_health(h)
    assert rec["last"]["error"]["type"] == "RuntimeError"
    assert rec["last"]["error"]["at"]
    assert on_disk(h)["attempts"] == 1
    h.mod._webui_poll_once()                        # cap reached -> abandon
    rec = read_health(h)
    assert rec["counters"]["abandoned"] == 1
    assert rec["last"]["abandoned_job"] == JID_A


# ---------------------------------------------------------------------------
# logging discipline
# ---------------------------------------------------------------------------

def test_poller_logs_start_once_and_stays_silent_when_idle(harness, caplog):
    h = harness
    with caplog.at_level(logging.DEBUG, logger="pi_worker"):
        h.mod._webui_poll_once()                    # init tick
    msgs = messages(caplog)
    assert sum(m.startswith(f"poller started pid={os.getpid()}")
               for m in msgs) == 1
    # at most one gate-seed transition on the init tick
    assert sum(m.startswith("gate pid=") for m in msgs) <= 1

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="pi_worker"):
        for _ in range(20):
            h.mod._webui_poll_once()                # idle: 20 real ticks
    assert messages(caplog) == []                   # ZERO per-tick spam


def test_delivery_lifecycle_logs_events_without_secrets(harness, caplog):
    h = harness
    make_job(h, JID_A, text=SENTINEL)
    with caplog.at_level(logging.DEBUG, logger="pi_worker"):
        h.mod._webui_poll_once()
        h.ledger[f"pi-bridge-{JID_A}-t1"]["delivery_state"] = "delivered"
        h.mod._webui_poll_once()
    msgs = messages(caplog)
    assert any(m == f"claim job={JID_A}" for m in msgs)
    assert any(m == f"injected job={JID_A}" for m in msgs)
    assert any(m == f"delivered job={JID_A}" for m in msgs)
    joined = "\n".join(msgs)
    assert SENTINEL not in joined
    assert SID not in joined


def test_gate_reason_transition_logs_once(harness, monkeypatch, caplog):
    h = harness
    monkeypatch.setattr(h.mod, "_webui_may_host", h.real_may_host)
    make_job(h)
    h.mod._webui_poll_once()          # init tick + one "no-host" evaluation
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="pi_worker"):
        for _ in range(5):
            h.mod._webui_poll_once()  # same reason, many ticks -> silence
        assert [m for m in messages(caplog)
                if m.startswith("gate pid=")] == []
        monkeypatch.setitem(sys.modules, "api.background_process",
                            type(sys)("api.background_process"))
        h.mod._webui_poll_once()      # the reason changes -> ONE line
    transitions = [m for m in messages(caplog) if m.startswith("gate pid=")]
    assert len(transitions) == 1
    assert "webui-positive" in transitions[0]


def test_error_logs_are_rate_limited_per_identical_message(harness, caplog):
    mod = harness.mod
    msg = "inject failed job=pb-x: RuntimeError"
    with caplog.at_level(logging.DEBUG, logger="pi_worker"):
        mod._log_limited(msg)
        mod._log_limited(msg)
        mod._log_limited(msg)
        assert sum(m == msg for m in messages(caplog)) == 1
        assert all(r.levelno == logging.WARNING for r in caplog.records)
        mod._log_seen[msg] -= 301                   # window expired
        mod._log_limited(msg)
        assert sum(m == msg for m in messages(caplog)) == 2


# ---------------------------------------------------------------------------
# the reader (pi_bridge/health.py)
# ---------------------------------------------------------------------------

def _dead_pid():
    """A pid guaranteed dead: a reaped child of this process."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = p.pid
    p.wait()
    return pid


def _write(d, pid, text):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"poller-{pid}.json").write_text(text, "utf-8")


def test_read_records_skips_dead_and_corrupt(tmp_path):
    d = tmp_path / "health"
    live = {"pid": os.getpid(), "role": "webui", "protocol": "V1.5"}
    _write(d, os.getpid(), json.dumps(live))
    dead = _dead_pid()
    _write(d, dead, json.dumps({"pid": dead, "role": "gateway"}))
    corrupt_pid = os.getppid()                      # alive, unparsable file
    _write(d, corrupt_pid, "{not json")
    _write(d, "not-a-pid", json.dumps({"pid": 1}))  # garbage name

    recs = health.read_records(tmp_path)
    assert [r["pid"] for r in recs] == [os.getpid()]
    # skipped files are left in place (only stale-by-age files are reaped)
    assert (d / f"poller-{dead}.json").is_file()
    assert (d / f"poller-{corrupt_pid}.json").is_file()


def test_read_records_cleans_stale_files(tmp_path):
    d = tmp_path / "health"
    p = _write(d, os.getpid(), json.dumps({"pid": os.getpid()})) or \
        d / f"poller-{os.getpid()}.json"
    old = time.time() - 25 * 3600
    os.utime(p, (old, old))
    assert health.read_records(tmp_path) == []
    assert not p.exists()                           # best-effort reaped


def test_read_records_name_pid_is_authoritative(tmp_path):
    d = tmp_path / "health"
    _write(d, os.getpid(), json.dumps({"role": "unknown"}))  # pid: from name
    recs = health.read_records(tmp_path)
    assert len(recs) == 1 and recs[0]["pid"] == os.getpid()


def test_read_records_missing_dir_is_empty(tmp_path):
    assert health.read_records(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# the CLI (`pi-bridge health`, end to end with PI_BRIDGE_HOME pointed at a
# fixture directory)
# ---------------------------------------------------------------------------

def test_cli_health_json_and_table_end_to_end(tmp_path, monkeypatch, capsys):
    d = tmp_path / "bh" / "health"
    rec = {"pid": os.getpid(), "plugin_version": "0.4.2", "protocol": "V1.5",
           "role": "webui", "may_host": True,
           "signals": {"gateway_run_loaded": False,
                       "api_background_process_loaded": True,
                       "tui_gateway_server_loaded": False},
           "started_at": "2026-01-01T00:00:00Z",
           "last_tick_at": "2026-01-01T00:05:00Z",
           "last_scan_at": "2026-01-01T00:05:00Z",
           "last_pending_count": 0, "last_gate_reason": "webui-positive",
           "pending_refused_count": 0,
           "last": {"injected_job": JID_A, "delivered_job": None,
                    "abandoned_job": None,
                    "error": {"type": "RuntimeError",
                              "at": "2026-01-01T00:04:00Z"}},
           "counters": {"ticks": 100, "scans": 100, "injected": 1,
                        "delivered": 0, "abandoned": 0, "gate_rejected": 0}}
    d.mkdir(parents=True)
    (d / f"poller-{os.getpid()}.json").write_text(json.dumps(rec), "utf-8")
    monkeypatch.setenv("PI_BRIDGE_HOME", str(tmp_path / "bh"))

    assert cli.main(["health", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1 and out[0]["role"] == "webui"
    assert out[0]["last"]["injected_job"] == JID_A

    assert cli.main(["health"]) == 0
    table = capsys.readouterr().out
    assert str(os.getpid()) in table
    assert "webui-positive" in table and JID_A in table
    assert "RuntimeError" in table


def test_cli_health_json_via_subprocess(tmp_path, monkeypatch):
    """True end-to-end: `python -m pi_bridge health --json` against a
    fixture PI_BRIDGE_HOME (also proves the subparser is wired)."""
    d = tmp_path / "bh" / "health"
    d.mkdir(parents=True)
    (d / f"poller-{os.getpid()}.json").write_text(
        json.dumps({"pid": os.getpid(), "role": "tui_backend",
                    "counters": {}, "last": {}}), "utf-8")
    monkeypatch.setenv("PI_BRIDGE_HOME", str(d.parent))
    r = bridge("health", "--json", check=0)
    out = json.loads(r.stdout)
    assert out[0]["pid"] == os.getpid()
    assert out[0]["role"] == "tui_backend"
