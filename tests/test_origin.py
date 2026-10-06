"""V1.3 origin capture: submit flags -> job.json -> status/list, plus the
plugin's session-context capture.  Origin is delivery metadata only: the
bridge stores it and echoes it back, never interprets it, and it must never
influence how pi is launched."""
import importlib.util
import json
import time

from conftest import FAKE_PI, REPO, bridge, job_json, submit, wait_terminal


def submit_raw(workdir, extra_argv, extra_env=None, check=0):
    return bridge("submit", "--cwd", str(workdir), "--task", "do the thing",
                  "--pi-bin", str(FAKE_PI), "--json", *extra_argv,
                  extra_env=extra_env, check=check)


def test_submit_with_origin_reaches_job_json_status_list(env, workdir):
    r = submit_raw(workdir, ["--origin-platform", "telegram",
                             "--origin-chat-id", "777000111",
                             "--origin-thread-id", "42",
                             "--origin-ui-session-id", "deadbeefcafe"])
    jid = json.loads(r.stdout)["job_id"]
    job = job_json(env["home"], jid)
    assert job["origin"] == {"platform": "telegram", "chat_id": "777000111",
                             "thread_id": "42", "ui_session_id": "deadbeefcafe"}
    st = json.loads(bridge("status", jid, "--json", check=0).stdout)
    assert st["origin"] == job["origin"]
    rows = json.loads(bridge("list", "--json", check=0).stdout)
    assert next(r for r in rows if r["job_id"] == jid)["origin"] == job["origin"]
    # origin survives the whole lifecycle (fake pi runs, feedback re-launches)
    final = wait_terminal(jid)
    assert final["status"] == "completed"
    assert final["origin"] == job["origin"]
    bridge("feedback", jid, "--feedback", "again", "--json", check=0)
    final = wait_terminal(jid)
    assert job_json(env["home"], jid)["origin"] == job["origin"]


def test_invalid_origin_values_are_dropped_not_fatal(env, workdir):
    r = submit_raw(workdir, ["--origin-platform", "Telegram X",  # uppercase+space
                             "--origin-chat-id", "123 456",      # space
                             "--origin-thread-id", "a;b",        # shell metachar
                             "--origin-ui-session-id", "тест"])  # non-ascii
    jid = json.loads(r.stdout)["job_id"]  # job created anyway
    job = job_json(env["home"], jid)
    assert "origin" not in job  # every field was invalid -> key absent
    st = json.loads(bridge("status", jid, "--json", check=0).stdout)
    assert st["origin"] is None
    final = wait_terminal(jid)
    assert final["status"] == "completed"


def test_partial_origin_keeps_only_valid_fields(env, workdir):
    r = submit_raw(workdir, ["--origin-platform", "webui",
                             "--origin-chat-id", "deadbeefcafe",
                             "--origin-thread-id", "",            # empty -> dropped
                             "--origin-ui-session-id", "bad id"])  # space -> dropped
    jid = json.loads(r.stdout)["job_id"]
    assert job_json(env["home"], jid)["origin"] == {
        "platform": "webui", "chat_id": "deadbeefcafe"}
    wait_terminal(jid)


def test_trailing_newline_origin_values_are_dropped(env, workdir):
    """`$` in Python also matches before a trailing newline; a value like
    'telegram\\n' must not survive into the field a woken run interpolates
    into `hermes send -t ...`."""
    r = submit_raw(workdir, ["--origin-platform=telegram\n",
                             "--origin-chat-id=777000111\n",
                             "--origin-ui-session-id=deadbeefcafe"])
    jid = json.loads(r.stdout)["job_id"]
    assert job_json(env["home"], jid)["origin"] == {
        "ui_session_id": "deadbeefcafe"}
    wait_terminal(jid)


def test_negative_chat_id_is_a_valid_origin(env, workdir):
    """Telegram group chat ids are negative; '-' is in the id charset, so
    such an origin must survive end to end (the plugin sends the inline
    --flag=value form precisely so argparse accepts these values)."""
    r = submit_raw(workdir, ["--origin-platform=telegram",
                             "--origin-chat-id=-1001234567890"])
    jid = json.loads(r.stdout)["job_id"]
    assert job_json(env["home"], jid)["origin"] == {
        "platform": "telegram", "chat_id": "-1001234567890"}
    st = json.loads(bridge("status", jid, "--json", check=0).stdout)
    assert st["origin"]["chat_id"] == "-1001234567890"
    wait_terminal(jid)


def test_oversize_origin_ids_are_dropped(env, workdir):
    jid_ok = json.loads(submit_raw(
        workdir, ["--origin-chat-id", "a" * 128]).stdout)["job_id"]
    assert len(job_json(env["home"], jid_ok)["origin"]["chat_id"]) == 128
    jid_big = json.loads(submit_raw(
        workdir, ["--origin-chat-id", "a" * 129,
                  "--origin-platform", "p" * 33]).stdout)["job_id"]  # >32 too
    assert "origin" not in job_json(env["home"], jid_big)
    wait_terminal(jid_ok)
    wait_terminal(jid_big)


def test_origin_flags_do_not_affect_pi_invocation(env, workdir, tmp_path):
    """fake pi must receive the exact same argv shape with or without origin;
    origin values never leak into the pi command line or the task text."""
    def run_with_dump(tag, origin_argv):
        dump = tmp_path / f"argv-{tag}.json"
        r = submit_raw(workdir, origin_argv,
                       extra_env={"FAKE_PI_ARGV_OUT": str(dump)})
        jid = json.loads(r.stdout)["job_id"]
        final = wait_terminal(jid)
        assert final["status"] == "completed"
        return json.loads(dump.read_text()), final

    plain_argv, plain_final = run_with_dump("plain", [])
    origin_argv, origin_final = run_with_dump(
        "origin", ["--origin-platform", "telegram",
                   "--origin-chat-id", "777000111"])
    # the bridge's pi invocation is a fixed 5-arg shape (argv[0] is the
    # interpreter line hidden by the fake), origin or not
    for argv in (plain_argv, origin_argv):
        assert len(argv) == 5
        assert argv[0:3] == ["--print", "--agent", "orchestrator"]
        assert argv[3] == "--session-id" and argv[4]
        assert "--origin-platform" not in " ".join(argv)
        assert "telegram" not in " ".join(argv)
        assert "777000111" not in " ".join(argv)
    assert plain_final["final_result"].startswith("FAKEOK")
    assert origin_final["final_result"].startswith("FAKEOK")


def _load_plugin(name):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "plugin" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _origin_recorder(tmp_path):
    """Fake pi-bridge CLI: records argv as JSON, answers with a fake job."""
    out = tmp_path / "cli-argv.json"
    script = tmp_path / "fake-pi-bridge-cli"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({str(out)!r}, 'w'))\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'job_id': 'pb-fake', 'status': 'queued'}))\n"
    )
    script.chmod(0o755)
    return script, out


def test_plugin_captures_session_env_into_flags(env, tmp_path, monkeypatch):
    for k in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID",
              "HERMES_SESSION_THREAD_ID", "HERMES_UI_SESSION_ID"):
        monkeypatch.delenv(k, raising=False)
    mod = _load_plugin("pi_worker_plugin_origin_env")
    monkeypatch.setattr(mod, "_get_session_env", None)  # force env fallback
    script, out = _origin_recorder(tmp_path)
    monkeypatch.setenv("PI_BRIDGE_CLI", str(script))

    # empty environment -> no origin flags at all
    res = json.loads(mod.pi_delegate({"task": "t", "cwd": str(tmp_path)}))
    assert res["job_id"] == "pb-fake"
    argv = json.loads(out.read_text())
    assert argv[:4] == ["submit", "--cwd", str(tmp_path), "--json"]
    assert not any(a.startswith("--origin") for a in argv)

    # populated environment -> exactly the four inline flags, appended
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "777000111")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "42")
    monkeypatch.setenv("HERMES_UI_SESSION_ID", "deadbeefcafe")
    mod.pi_delegate({"task": "t", "cwd": str(tmp_path)})
    argv = json.loads(out.read_text())
    assert argv[4:] == ["--origin-platform=telegram",
                        "--origin-chat-id=777000111",
                        "--origin-thread-id=42",
                        "--origin-ui-session-id=deadbeefcafe"]


def test_plugin_prefers_get_session_env_over_env(env, tmp_path, monkeypatch):
    """In-process (gateway) the contextvar-first getter wins; an empty
    context returns no flags even when the environment looks populated."""
    for k, v in (("HERMES_SESSION_PLATFORM", "telegram"),
                 ("HERMES_SESSION_CHAT_ID", "9"),
                 ("HERMES_UI_SESSION_ID", "stale-ui")):
        monkeypatch.setenv(k, v)
    mod = _load_plugin("pi_worker_plugin_origin_ctx")
    monkeypatch.setattr(mod, "_get_session_env", lambda name, default="": {
        "HERMES_SESSION_PLATFORM": "webui",
        "HERMES_UI_SESSION_ID": "deadbeefcafe",
    }.get(name, default))
    script, out = _origin_recorder(tmp_path)
    monkeypatch.setenv("PI_BRIDGE_CLI", str(script))

    mod.pi_delegate({"task": "t", "cwd": str(tmp_path)})
    argv = json.loads(out.read_text())
    assert argv[4:] == ["--origin-platform=webui",
                        "--origin-ui-session-id=deadbeefcafe"]

    # context present but fully cleared -> nothing is passed (webui-less turn)
    monkeypatch.setattr(mod, "_get_session_env", lambda name, default="": "")
    mod.pi_delegate({"task": "t", "cwd": str(tmp_path)})
    argv = json.loads(out.read_text())
    assert not any(a.startswith("--origin") for a in argv)


def test_plugin_delegate_end_to_end_records_origin(env, workdir, monkeypatch):
    """handler -> real CLI -> fake pi: the job state carries the origin."""
    for k in ("HERMES_SESSION_THREAD_ID", "HERMES_UI_SESSION_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webui")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("PI_BRIDGE_CLI", str(REPO / ".venv" / "bin" / "pi-bridge"))
    monkeypatch.setenv("PI_BRIDGE_PI_BIN", str(FAKE_PI))
    monkeypatch.setenv("PI_BRIDGE_LAUNCHER", "detach")
    mod = _load_plugin("pi_worker_plugin_origin_e2e")
    monkeypatch.setattr(mod, "_get_session_env", None)  # env fallback path
    out = json.loads(mod.pi_delegate({"task": "via plugin", "cwd": str(workdir)}))
    jid = out["job_id"]
    job = job_json(env["home"], jid)
    assert job["origin"] == {"platform": "webui", "chat_id": "-1001234567890"}
    deadline = time.time() + 20
    while time.time() < deadline:
        v = json.loads(mod.pi_status({"job_id": jid}))
        if v["status"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    assert v["status"] == "completed"
    assert v["origin"] == job["origin"]


def test_legacy_job_without_origin_still_works(env, workdir):
    """Old job.json shapes (no origin key at all) load, list and status fine."""
    view = submit(env, workdir, task="legacy shape")
    jid = view["job_id"]
    wait_terminal(jid)
    job = job_json(env["home"], jid)
    assert "origin" not in job  # submitted without flags -> key never written
    st = json.loads(bridge("status", jid, "--json", check=0).stdout)
    assert st["origin"] is None
    assert st["status"] == "completed"
    rows = json.loads(bridge("list", "--json", check=0).stdout)
    assert next(r for r in rows if r["job_id"] == jid)["origin"] is None
