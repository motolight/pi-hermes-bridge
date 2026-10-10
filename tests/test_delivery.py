"""V1.4 origin delivery (pi_bridge/deliver.py) tests.

The regression these tests exist for (documented incident, 2026-10-07, job
pb-20261007T200850Z-d49a4c): a job with origin=webui finished, the wake POST
was accepted (`wake.delivered == true`), but the outcome was delivered
NOWHERE -- the woken Hermes run got its delivery command stuck in the
dangerous-command approval gate (a webhook session has no one to approve, so
the gate blocks for `approvals.timeout` and denies fail-closed), and its
final reply went to the route's `deliver: log` sink.  `wake.delivered: true`
while the user saw nothing.

The fix moves user-visible delivery out of the model's shell and into the
runner: an argv-only `hermes` subprocess, strictly by origin, hard timeout,
no retries, no fallback channel.  Nothing here ever calls the real hermes
CLI: tests point PI_BRIDGE_HERMES_BIN at tests/fake_hermes.py, which records
the argv it received and can emulate the CLI's refusals.
"""
import json
import socket
import time
from pathlib import Path

from conftest import FAKE_PI, REPO, bridge, job_json, wait_terminal
from fake_webhook import start_receiver

FAKE_HERMES = REPO / "tests" / "fake_hermes.py"
WEBUI_SID = "ee58f3039666"


def hermes_env(tmp, mode="ok", extra=None):
    """Env for a run: fake hermes CLI recording into <tmp>/hermes-argv.jsonl."""
    argv_log = tmp / "hermes-argv.jsonl"
    e = {"PI_BRIDGE_HERMES_BIN": str(FAKE_HERMES),
         "FAKE_HERMES_ARGV_OUT": str(argv_log),
         "FAKE_HERMES_MODE": mode}
    e.update(extra or {})
    return e, argv_log


def submit(env, workdir, origin=(), extra_env=None, task="do the thing"):
    r = bridge("submit", "--cwd", str(workdir), "--task", task,
               "--pi-bin", str(FAKE_PI), "--json", *origin,
               extra_env=extra_env or {}, check=0)
    return json.loads(r.stdout)


def read_calls(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def wait_delivery(job_id, extra_env=None, timeout=25):
    """Poll status until the runner recorded a delivery attempt (or reason)."""
    deadline = time.time() + timeout
    st = None
    while time.time() < deadline:
        st = json.loads(bridge("status", job_id, "--json", check=0,
                              extra_env=extra_env or {}).stdout)
        d = st["delivery"]
        if d["attempted"] or d["reason"]:
            return st
        time.sleep(0.2)
    raise AssertionError(f"delivery never recorded: {st}")


WEBUI_ORIGIN = ("--origin-platform", "webui",
                "--origin-ui-session-id", WEBUI_SID,
                "--origin-chat-id", WEBUI_SID)


# ---------------------------------------------------------------------------
# the incident, end to end
# ---------------------------------------------------------------------------

def test_incident_replay_webui_origin_plus_blocked_gate_still_delivers(env, workdir, tmp_path):
    """wake accepted AND the outcome delivered to the webui session.

    `mode=blocked` models the CLI itself refusing (the approval-gate text the
    agent got in the incident); the bridge must record the failure without
    retrying, without inventing another channel, and without touching the
    job status.  The success half is the separate test below; here we assert
    the *attempt* is made by the bridge -- i.e. delivery no longer depends on
    the wake run's shell.
    """
    e, log = hermes_env(tmp_path, mode="blocked")
    view = submit(env, workdir, WEBUI_ORIGIN, extra_env=e)
    jid = view["job_id"]

    st = wait_delivery(jid, extra_env=e)
    calls = read_calls(log)

    # The bridge itself invoked the resume command for the ORIGIN session...
    assert len(calls) == 1, calls
    argv = calls[0]           # the fake records its own argv[1:]: no binary path
    assert argv[0:2] == ["--resume", WEBUI_SID]
    assert argv[2] == "chat" and "-q" in argv and "-Q" in argv
    assert argv[-2:] == ["--source", "tool"]
    # ...with the outcome text as ONE opaque argument (no shell: the text
    # cannot be re-read as a command line, which is what tripped the gate).
    body = argv[argv.index("-q") + 1]
    assert jid in body and "FAKEOK" in body
    # The blocked attempt is data: reason recorded, no retry, no fallback.
    assert st["delivery"]["attempted"] is True
    assert st["delivery"]["ok"] is False
    assert st["delivery"]["channel"] == f"webui:{WEBUI_SID}"
    assert st["delivery"]["reason"] == "cli-failed"
    assert len(read_calls(log)) == 1, "delivery must never retry"
    assert st["status"] == "completed"


def test_webui_origin_delivered_to_resumed_session(env, workdir, tmp_path):
    e, log = hermes_env(tmp_path)
    jid = submit(env, workdir, WEBUI_ORIGIN, extra_env=e)["job_id"]

    st = wait_delivery(jid, extra_env=e)
    assert st["status"] == "completed"
    d = st["delivery"]
    assert (d["attempted"], d["ok"], d["kind"], d["channel"], d["reason"],
            d["error"], d["turn"]) == (
        True, True, "webui-resume", f"webui:{WEBUI_SID}", "ok", None, 1)
    assert st["delivery"]["at"]
    calls = read_calls(log)
    assert len(calls) == 1
    # the delivered text carries the header and pi's result
    body = calls[0][calls[0].index("-q") + 1]
    assert "Pi-задача" in body and jid in body and "FAKEOK" in body
    # and the durable status view agrees
    assert job_json(env["home"], jid)["delivery"]["ok"] is True


def test_delivery_survives_a_dead_wake_channel(env, workdir, tmp_path):
    """The wake POST (and the woken run) is not the delivery path.

    The webhook endpoint is a closed port -> the notifier retries; delivery
    must already have happened and the job must stay `completed`.
    """
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens here -> the notifier retries, delivery did not
    (env["home"] / "wake.json").write_text(json.dumps(
        {"enabled": True, "url": f"http://127.0.0.1:{port}/webhooks/x",
         "secret": "s" * 16}))
    e, log = hermes_env(tmp_path)
    jid = submit(env, workdir, WEBUI_ORIGIN, extra_env={
        **e, "PI_BRIDGE_WAKE_INTERVAL": "0.3",
        "PI_BRIDGE_WAKE_MAX_ATTEMPTS": "3",
        "PI_BRIDGE_WAKE_HTTP_TIMEOUT": "1"})["job_id"]

    st = wait_delivery(jid)
    assert st["delivery"]["ok"] is True
    assert len(read_calls(log)) == 1
    assert st["wake"]["delivered"] is False
    assert st["status"] == "completed"


def test_wake_and_delivery_are_distinct_successes(env, workdir, tmp_path):
    """wake.delivered==true means 'an agent run started', nothing more."""
    srv = start_receiver(secret="s" * 16)
    url = f"http://127.0.0.1:{srv.server_address[1]}/webhooks/pi-bridge-complete"
    (env["home"] / "wake.json").write_text(json.dumps(
        {"enabled": True, "url": url, "secret": "s" * 16}))
    e, log = hermes_env(tmp_path)
    jid = submit(env, workdir, WEBUI_ORIGIN, extra_env={
        **e, "PI_BRIDGE_WAKE_INTERVAL": "0.3"})["job_id"]

    deadline = time.time() + 25
    while time.time() < deadline:
        st = json.loads(bridge("status", jid, "--json", check=0,
                              extra_env=e).stdout)
        if st["wake"]["delivered"] and st["delivery"]["attempted"]:
            break
        time.sleep(0.2)
    assert st["wake"]["delivered"] is True
    assert st["delivery"]["ok"] is True
    assert len(srv.events) == 1
    # the wake payload itself carries origin + the delivery view, so a woken
    # run can see "the user has already been told" without calling pi_status
    body = srv.events[0]["body"]
    assert body["job_id"] == jid
    assert body["origin"]["platform"] == "webui"
    assert body["delivery"] == {"attempted": True, "ok": True,
                                "channel": f"webui:{WEBUI_SID}",
                                "reason": "ok"}
    srv.shutdown()


# ---------------------------------------------------------------------------
# routing policy
# ---------------------------------------------------------------------------

def test_messaging_origin_uses_send_with_thread(env, workdir, tmp_path):
    e, log = hermes_env(tmp_path)
    jid = submit(env, workdir, ("--origin-platform", "telegram",
                                "--origin-chat-id", "111111111",
                                "--origin-thread-id", "4242"),
                 extra_env=e)["job_id"]
    st = wait_delivery(jid, extra_env=e)
    assert st["delivery"]["ok"] is True and st["delivery"]["kind"] == "send"
    argv = read_calls(log)[0]
    assert argv[0:3] == ["send", "-t", "telegram:111111111:4242"]
    assert argv[-1] == "-q"


def test_messaging_origin_without_thread(env, workdir, tmp_path):
    e, log = hermes_env(tmp_path)
    jid = submit(env, workdir, ("--origin-platform", "matrix",
                                "--origin-chat-id", "room123"),
                 extra_env=e)["job_id"]
    st = wait_delivery(jid, extra_env=e)
    assert st["delivery"]["channel"] == "matrix:room123"
    assert read_calls(log)[0][2] == "matrix:room123"


def test_telegram_is_never_a_fallback_for_webui_origin(env, workdir, tmp_path):
    """A webui job must produce exactly one call, and it is the resume."""
    e, log = hermes_env(tmp_path, mode="refused")
    jid = submit(env, workdir, WEBUI_ORIGIN, extra_env=e)["job_id"]
    st = wait_delivery(jid, extra_env=e)
    assert st["delivery"]["reason"] == "session_not_owned"
    assert st["delivery"]["ok"] is False
    calls = read_calls(log)
    assert len(calls) == 1 and "send" not in calls[0]


def test_no_origin_delivers_nowhere(env, workdir, tmp_path):
    e, log = hermes_env(tmp_path)
    jid = submit(env, workdir, (), extra_env=e)["job_id"]
    st = wait_delivery(jid, extra_env=e)
    assert st["status"] == "completed"
    assert st["delivery"]["reason"] == "no-origin"
    assert st["delivery"]["ok"] is False
    assert read_calls(log) == []


def test_local_and_unknown_platforms_deliver_nowhere(env, workdir, tmp_path):
    e, log = hermes_env(tmp_path)
    for platform in ("local", "smoking_tube"):
        jid = submit(env, workdir, ("--origin-platform", platform,
                                    "--origin-chat-id", "x1"),
                     extra_env=e)["job_id"]
        st = wait_delivery(jid, extra_env=e)
        assert st["delivery"]["reason"] == "no-delivery-channel", st
    assert read_calls(log) == []


def test_webui_origin_without_session_id_delivers_nowhere(env, workdir, tmp_path):
    e, log = hermes_env(tmp_path)
    jid = submit(env, workdir, ("--origin-platform", "webui",
                                "--origin-chat-id", "b ad"),  # invalid -> dropped
                 extra_env=e)["job_id"]
    st = wait_delivery(jid, extra_env=e)
    assert st["delivery"]["reason"] == "no-webui-session-id"
    assert read_calls(log) == []


def test_missing_hermes_binary_is_reported_not_fatal(env, workdir):
    e = {"PI_BRIDGE_HERMES_BIN": "/does/not/exist/hermes"}
    jid = submit(env, workdir, WEBUI_ORIGIN, extra_env=e)["job_id"]
    st = wait_delivery(jid, extra_env=e)
    assert st["status"] == "completed"
    assert st["delivery"]["reason"] == "hermes-binary-not-found"


def test_origin_delivery_can_be_disabled(env, workdir, tmp_path):
    e, log = hermes_env(tmp_path)
    (env["home"] / "wake.json").write_text(json.dumps(
        {"enabled": False, "origin_delivery": False}))
    jid = submit(env, workdir, WEBUI_ORIGIN, extra_env=e)["job_id"]
    st = wait_terminal(jid, extra_env=e)
    assert st["status"] == "completed"
    assert st["delivery"]["attempted"] is False
    assert read_calls(log) == []


# ---------------------------------------------------------------------------
# hard timeout, and the failure can never break a job
# ---------------------------------------------------------------------------

def test_delivery_hard_timeout_does_not_hang_the_runner(env, workdir, tmp_path):
    e, log = hermes_env(tmp_path, mode="sleep",
                        extra={"FAKE_HERMES_SLEEP": "30",
                               "PI_BRIDGE_DELIVERY_TIMEOUT": "1"})
    jid = submit(env, workdir, WEBUI_ORIGIN, extra_env=e)["job_id"]
    started = time.time()
    st = wait_delivery(jid, extra_env=e, timeout=20)
    assert time.time() - started < 25
    assert st["delivery"]["reason"] == "timeout"
    assert st["delivery"]["ok"] is False
    assert st["status"] == "completed"
    assert len(read_calls(log)) == 1


def test_failed_turn_delivers_the_error(env, workdir, tmp_path):
    e, log = hermes_env(tmp_path)
    jid = submit(env, workdir, WEBUI_ORIGIN, extra_env=e,
                 task="BRIDGE_FAIL")["job_id"]
    st = wait_delivery(jid, extra_env=e)
    assert st["status"] == "failed"
    assert st["delivery"]["ok"] is True
    body = read_calls(log)[0][4]
    assert "завершилась ошибкой" in body and "boom" in body


def test_cancelled_job_delivers_nothing(env, workdir, tmp_path):
    e, log = hermes_env(tmp_path)
    jid = submit(env, workdir, WEBUI_ORIGIN, extra_env=e,
                 task="BRIDGE_SLEEP 30")["job_id"]
    bridge("cancel", jid, "--json", check=0, extra_env=e)
    st = wait_terminal(jid, extra_env=e)
    assert st["status"] == "cancelled"
    time.sleep(0.5)
    assert read_calls(log) == []


# ---------------------------------------------------------------------------
# unit: text building and planning (no subprocess)
# ---------------------------------------------------------------------------

def test_build_text_sanitizes_attachment_markers(env, monkeypatch):
    from pi_bridge import deliver

    monkeypatch.setenv("PI_BRIDGE_HOME", str(env["home"]))
    job = {"job_id": "j1", "status": "completed", "task": "t", "turns": [{}],
           "final_result": "MEDIA:/etc/passwd\n[[as_document]]\nok"}
    text = deliver.build_text(job, 1200)
    assert "MEDIA:" not in text.replace("[MEDIA:]", "")
    assert "[[as_document]]" not in text
    assert "ok" in text


def test_build_text_never_starts_like_a_flag(env, monkeypatch):
    from pi_bridge import deliver

    monkeypatch.setenv("PI_BRIDGE_HOME", str(env["home"]))
    job = {"job_id": "j1", "status": "completed", "task": "-rf /",
           "turns": [{}], "final_result": "-q nope"}
    assert not deliver.build_text(job, 1200).startswith("-")


def test_plan_uses_argv_never_shell(env, monkeypatch):
    """A result that LOOKS like a dangerous command is inert: one argv element."""
    from pi_bridge import deliver

    monkeypatch.setenv("PI_BRIDGE_HOME", str(env["home"]))
    job = {"job_id": "j1", "status": "completed", "task": "t", "turns": [{}],
           "final_result": "systemctl restart nginx && rm -rf /",
           "origin": {"platform": "webui", "ui_session_id": "abc123"}}
    text = deliver.build_text(job, 1200)
    p = deliver.plan(job, str(FAKE_HERMES), text)
    assert p["kind"] == "webui-resume"
    assert p["argv"].count(text) == 1  # the whole text is a single argument
    assert not any("&" in a or "&&" in a for a in p["argv"] if a != text)


def test_delivery_state_is_backward_compatible(env, monkeypatch):
    """Old job.json files (V1.3, no `delivery` key) still produce a view."""
    from pi_bridge import deliver

    monkeypatch.setenv("PI_BRIDGE_HOME", str(env["home"]))
    assert deliver.normalize_state({}) == deliver.blank_state()
    assert deliver.normalize_state({"delivery": "garbage"}) == deliver.blank_state()


def test_feedback_turn_delivers_again(env, workdir, tmp_path):
    e, log = hermes_env(tmp_path)
    jid = submit(env, workdir, WEBUI_ORIGIN, extra_env=e)["job_id"]
    wait_delivery(jid, extra_env=e)
    bridge("feedback", jid, "--feedback", "one more turn", "--json",
           check=0, extra_env=e)
    deadline = time.time() + 25
    while time.time() < deadline:
        st = json.loads(bridge("status", jid, "--json", check=0,
                              extra_env=e).stdout)
        if st["delivery"]["turn"] == 2:
            break
        time.sleep(0.2)
    assert st["delivery"]["turn"] == 2 and st["delivery"]["ok"] is True
    assert len(read_calls(log)) == 2
