"""v0.4.1 task fidelity: the bridge never truncates, and never launches a
compaction-mangled task.

Two halves (docs/WEBUI_COMPLETION_EVENT_SPEC.md, "Truncation"):

* the *good* path is byte-exact across every boundary the bridge owns --
  plugin handler -> CLI submit (stdin) -> job.json["task"] ->
  turns/01.task.txt -> the pi child's stdin (which tests/fake_pi.py records
  into its session file).  A >= 50 KB task with a unique sentinel at the very
  end must arrive complete: the incident's truncated task was 436 chars, so
  any silent cut is caught by the sentinel.
* the *bad* path: Hermes history compaction rewrites old tool-call arguments
  to end with "...[truncated]" and a model can copy that verbatim into
  pi_delegate.  The bridge cannot restore it, so it REFUSES (actionable
  "task-text-truncated" error, no job created) instead of starting a doomed
  job.
"""
import importlib.util
import json
from pathlib import Path

import pytest

from conftest import FAKE_PI, REPO, bridge, job_json, wait_terminal

SENTINEL = "PI_BRIDGE_TASK_END_SENTINEL_4A91"


def _load_plugin(name="pi_worker_plugin_fidelity"):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "plugin" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def plugin_cli(env, monkeypatch):
    """The plugin talks to the real bridge CLI + the fake pi executable."""
    monkeypatch.setenv("PI_BRIDGE_CLI", str(REPO / ".venv" / "bin" / "pi-bridge"))
    monkeypatch.setenv("PI_BRIDGE_PI_BIN", str(FAKE_PI))
    (env["home"] / "jobs").mkdir(exist_ok=True)
    return _load_plugin()


def job_count(env) -> int:
    jobs = env["home"] / "jobs"
    return len([p for p in jobs.iterdir() if p.is_dir()]) if jobs.is_dir() else 0


def long_task(min_chars: int = 50_000) -> str:
    """A >= 50 KB multi-line task that ENDS with the unique sentinel."""
    lines = []
    for i in range(900):
        lines.append(f"шаг {i:04d}: " + "рефакторинг парсера " * 4)
    task = "\n".join(lines) + f"\n\nFINISH WITH THIS EXACT TAIL: {SENTINEL}"
    assert len(task) >= min_chars, len(task)
    assert task.endswith(SENTINEL)
    return task


# ---------------------------------------------------------------------------
# (a) the full task survives every bridge-owned boundary
# ---------------------------------------------------------------------------

def test_long_task_round_trips_byte_identical(plugin_cli, env, workdir):
    task = long_task()

    view = json.loads(plugin_cli.pi_delegate({"task": task,
                                              "cwd": str(workdir)}))
    assert "job_id" in view, view
    jid = view["job_id"]
    final = wait_terminal(jid, timeout=40)
    assert final["status"] == "completed", final

    # boundary 1: the persisted job state
    job = job_json(env["home"], jid)
    assert job["task"] == task
    assert job["task"].encode("utf-8") == task.encode("utf-8")
    assert job["task"].endswith(SENTINEL)
    assert len(job["task"]) == len(task)

    # boundary 2: the turn transcript written for the runner
    turn_file = Path(env["home"]) / "jobs" / jid / "turns" / "01.task.txt"
    on_disk = turn_file.read_text("utf-8")
    assert on_disk == task
    assert on_disk.endswith(SENTINEL)

    # boundary 3: what pi actually read on stdin (fake_pi records it)
    session = Path(job["pi_session_file"])
    assert session.is_file()
    entries = [json.loads(line) for line in
               session.read_text("utf-8").splitlines() if line.strip()]
    user_texts = [e["text"] for e in entries if e.get("role") == "user"]
    assert user_texts, entries
    assert user_texts[0] == task
    assert user_texts[0].endswith(SENTINEL)

    # boundary 4: pi's own echo of the byte count it received
    assert f"task_len={len(task)}" in (job.get("final_result") or "")


def test_cli_submit_stdin_is_the_plugin_path(env, workdir):
    """The CLI reads the task from stdin verbatim (what plugin/_run uses)."""
    task = long_task()
    r = bridge("submit", "--cwd", str(workdir), "--pi-bin", str(FAKE_PI),
               "--json", input=task, check=0)
    jid = json.loads(r.stdout)["job_id"]
    wait_terminal(jid, timeout=40)
    job = job_json(env["home"], jid)
    assert job["task"] == task and job["task"].endswith(SENTINEL)
    assert (Path(env["home"]) / "jobs" / jid / "turns" /
            "01.task.txt").read_text("utf-8") == task


def test_multibyte_task_survives_the_json_roundtrip(plugin_cli, env, workdir):
    task = "🎨 ünïcødе \\ back\"tick\n«quote» " * 2000 + SENTINEL
    view = json.loads(plugin_cli.pi_delegate({"task": task,
                                              "cwd": str(workdir)}))
    jid = view["job_id"]
    wait_terminal(jid, timeout=40)
    job = job_json(env["home"], jid)
    assert job["task"] == task and job["task"].endswith(SENTINEL)


# ---------------------------------------------------------------------------
# (b) the compaction-marker guard
# ---------------------------------------------------------------------------

TRUNCATED_ENDINGS = [
    "refactor the parser so it keeps comments… («... [truncated]",
    'foo ...[truncated]"',
    "bar [truncated]».",
    "... [TRUNCATED]  ",
    "do the thing [truncated].",
    "line one\nline two … [ truncated ]",
]
INTACT_ENDINGS = [
    "",
    "read the file and fix the parser",
    "the run finished ok.",
    "... [tool result]",
    "we saw [truncated] mid-text but the task continues to its real end",
    "ends with a bracket]",
    "ends with a quote\"",
    "[truncated] was only the beginning of the instruction",
]


@pytest.mark.parametrize("task", TRUNCATED_ENDINGS)
def test_guard_flags_compaction_markers(plugin_cli, task):
    assert plugin_cli._looks_compaction_truncated(task) is True


@pytest.mark.parametrize("task", INTACT_ENDINGS)
def test_guard_leaves_intact_text_alone(plugin_cli, task):
    assert plugin_cli._looks_compaction_truncated(task) is False


# ---------------------------------------------------------------------------
# (c) pi_delegate refuses a truncated task and creates nothing
# ---------------------------------------------------------------------------

def test_pi_delegate_refuses_truncated_task_and_creates_no_job(plugin_cli, env,
                                                               workdir):
    before = job_count(env)
    for bad in TRUNCATED_ENDINGS:
        out = json.loads(plugin_cli.pi_delegate({"task": bad,
                                                 "cwd": str(workdir)}))
        assert "task-text-truncated" in out["error"], (bad, out)
        assert "full" in out["error"].lower() or "FULL" in out["error"]
    assert job_count(env) == before, "a refused task must not create a job"

    # the same handler still accepts a complete task of the same shape
    ok = json.loads(plugin_cli.pi_delegate(
        {"task": "refactor the parser, keep comments, run the suite.",
         "cwd": str(workdir)}))
    assert "job_id" in ok, ok
    assert job_count(env) == before + 1
    wait_terminal(ok["job_id"], timeout=40)


def test_pi_delegate_rejects_empty_task_before_the_guard(plugin_cli, env,
                                                         workdir):
    out = json.loads(plugin_cli.pi_delegate({"task": "   ", "cwd": "."}))
    assert out["error"] == "task is required"
    assert job_count(env) == 0
