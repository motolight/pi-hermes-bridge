"""pi-worker: Hermes user plugin exposing the durable pi-bridge as 5 tools.

Thin adapter only: every tool shells out (argv, no shell) to the standalone
`pi-bridge` CLI. The plugin never talks to pi directly and never selects Pi
subagents -- the Pi orchestrator decides that itself. No privileged
capabilities are declared or used.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

# Portable CLI discovery: explicit env > PATH. No hardcoded home paths; the
# bridge CLI's own pi-binary resolution (env > bridge config.json > PATH)
# handles the rest.
_PI_BRIDGE_CLI_PATHS = [
    os.path.expanduser("~/.local/bin/pi-bridge"),
    "/usr/local/bin/pi-bridge",
]
_SUBMIT_TIMEOUT = 30
_QUERY_TIMEOUT = 20


def _cli() -> str:
    cli = os.environ.get("PI_BRIDGE_CLI") or shutil.which("pi-bridge") or ""
    if not cli:
        for cand in _PI_BRIDGE_CLI_PATHS:
            if os.path.isfile(cand):
                cli = cand
                break
    return cli

# ---------------------------------------------------------------------------
# Delivery-origin capture (V1.3).  The handler runs INSIDE the Hermes agent
# process, so the gateway's contextvar-first session context is authoritative
# there; outside a gateway process (plain CLI, tests) we fall back to
# os.environ.  The bridge only STORES this metadata and returns it in
# status/list -- it never interprets it or delivers anything itself.
# ---------------------------------------------------------------------------
try:  # available when loaded by the Hermes agent from the Hermes home
    from gateway.session_context import get_session_env as _get_session_env
except Exception:  # any import failure -> plain env fallback (never fatal)
    _get_session_env = None

_ORIGIN_ENV_KEYS = (
    ("platform", "HERMES_SESSION_PLATFORM"),
    ("chat_id", "HERMES_SESSION_CHAT_ID"),
    ("thread_id", "HERMES_SESSION_THREAD_ID"),
    ("ui_session_id", "HERMES_UI_SESSION_ID"),
)
_ORIGIN_FLAGS = {
    "platform": "--origin-platform",
    "chat_id": "--origin-chat-id",
    "thread_id": "--origin-thread-id",
    "ui_session_id": "--origin-ui-session-id",
}


def _session_env(name: str) -> str:
    if _get_session_env is not None:
        try:
            return _get_session_env(name, "") or ""
        except Exception:
            pass  # defensive: origin capture must never break a delegate
    return os.environ.get(name, "") or ""


def _capture_origin() -> dict:
    """Fixed 4-key delivery origin; empty fields are omitted."""
    origin = {}
    for key, env_name in _ORIGIN_ENV_KEYS:
        val = _session_env(env_name).strip()
        if val:
            origin[key] = val
    return origin


def _origin_argv(origin: dict) -> list[str]:
    """CLI flags for the non-empty origin fields (argv list, never a shell).

    Inline ``--flag=value`` form: valid ids may start with '-' (negative
    Telegram chat ids), and argparse only accepts those as values when the
    value is attached to the flag.
    """
    argv: list[str] = []
    for key, flag in _ORIGIN_FLAGS.items():
        val = origin.get(key)
        if val:
            argv.append(f"{flag}={val}")
    return argv


def _run(argv: list[str], stdin_text: str | None = None,
         timeout: int = _QUERY_TIMEOUT) -> str:
    cli = _cli()
    if not os.path.isfile(cli):
        return json.dumps({"error": f"pi-bridge CLI not found: {cli} "
                                    "(set PI_BRIDGE_CLI)"})
    try:
        r = subprocess.run(
            [cli, *argv],
            input=stdin_text,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"pi-bridge {argv[0]} timed out"})
    except OSError as e:
        return json.dumps({"error": f"cannot run pi-bridge: {e}"})
    if r.returncode == 0:
        try:
            return json.dumps(json.loads(r.stdout))
        except json.JSONDecodeError:
            return json.dumps({"ok": True, "output": r.stdout.strip()})
    try:
        err = json.loads(r.stderr)
    except json.JSONDecodeError:
        err = {"error": (r.stderr or r.stdout).strip()[:2000]}
    return json.dumps(err)


def _safe(fn):
    """Guarantee the Hermes handler contract: accepts **kwargs, always
    returns a JSON string, never raises."""

    def wrapper(args, **kwargs):
        if not isinstance(args, dict):
            return json.dumps({"error": "invalid arguments: expected an object"})
        try:
            return fn(args, **kwargs)
        except Exception as e:  # defensive: Hermes converts raises to generic errors
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

    return wrapper


def pi_delegate(args: dict, **kwargs) -> str:
    task = args.get("task") or ""
    cwd = args.get("cwd") or ""
    if not task.strip():
        return json.dumps({"error": "task is required"})
    if not cwd:
        return json.dumps({"error": "cwd is required (absolute path)"})
    argv = ["submit", "--cwd", cwd, "--json"] + _origin_argv(_capture_origin())
    return _run(argv, stdin_text=task, timeout=_SUBMIT_TIMEOUT)


def pi_list(args: dict, **kwargs) -> str:
    argv = ["list", "--json"]
    limit = args.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return json.dumps({"error": "limit must be an integer"})
        if limit < 0:
            return json.dumps({"error": "limit must be >= 0"})
        argv += ["--limit", str(limit)]
    return _run(argv)


def pi_status(args: dict, **kwargs) -> str:
    job_id = args.get("job_id") or ""
    if not job_id:
        return json.dumps({"error": "job_id is required"})
    return _run(["status", job_id, "--json"])


def pi_feedback(args: dict, **kwargs) -> str:
    job_id = args.get("job_id") or ""
    text = args.get("feedback") or ""
    if not job_id:
        return json.dumps({"error": "job_id is required"})
    if not text.strip():
        return json.dumps({"error": "feedback is required"})
    return _run(["feedback", job_id, "--json"], stdin_text=text,
                timeout=_SUBMIT_TIMEOUT)


def pi_cancel(args: dict, **kwargs) -> str:
    job_id = args.get("job_id") or ""
    if not job_id:
        return json.dumps({"error": "job_id is required"})
    return _run(["cancel", job_id, "--json"])


_SCHEMAS = {
    "pi_delegate": {
        "name": "pi_delegate",
        "description": (
            "Delegate a coding task to the Pi orchestrator. Returns "
            "immediately with a job_id; the task runs durably in the "
            "background. The Pi orchestrator manages its own subagents. "
            "Poll with pi_status; refine with pi_feedback. When a turn of "
            "the job ends, the bridge delivers the outcome back along the "
            "current session's recorded origin (platform/chat id/"
            "ui_session_id) itself and wakes a Hermes acceptance run by "
            "itself (pi_bridge_turn_complete) -- you do not have to poll, "
            "and you must NOT send the result yourself. "
            "If you don't have a job_id or are unsure a previous delegate "
            "succeeded, call pi_list FIRST and match by task/cwd -- do NOT "
            "blindly re-delegate the same work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "Full task description for the Pi orchestrator"},
                "cwd": {"type": "string",
                        "description": "Absolute path of the working directory for the Pi job (must exist)"},
            },
            "required": ["task", "cwd"],
        },
    },
    "pi_list": {
        "name": "pi_list",
        "description": (
            "List recent pi-bridge jobs, newest first: job_id, status, cwd, "
            "created_at/updated_at, task_preview, turn counts, whether the "
            "completion wake was delivered and whether the outcome was "
            "delivered to its origin channel. No transcripts. Use this to "
            "recover job ids after a restart or a context loss, and BEFORE "
            "delegating when you are unsure an earlier pi_delegate succeeded "
            "-- match by task/cwd instead of re-delegating the same work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                          "description": "Max rows to return, newest first (default 20)"},
            },
            "required": [],
        },
    },
    "pi_status": {
        "name": "pi_status",
        "description": (
            "Get status of a delegated Pi job: queued/running/completed/"
            "failed/cancelled/interrupted, plus a compact final result when "
            "completed, the wake-notifier state, the origin-delivery result "
            "(did the outcome actually reach the requesting channel) and "
            "the recorded delivery origin {platform, chat_id, thread_id, "
            "ui_session_id} of the requesting session (or null). Does not "
            "include Pi's internal transcript."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job id returned by pi_delegate"},
            },
            "required": ["job_id"],
        },
    },
    "pi_feedback": {
        "name": "pi_feedback",
        "description": (
            "Send feedback to an existing Pi job so the SAME Pi session "
            "(full context) fixes or extends the result. Only accepted when "
            "the job is terminal (completed/failed/cancelled/interrupted). "
            "The job goes back to running, and the completion of that new "
            "turn wakes Hermes again -- so after pi_feedback you may end "
            "your turn."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job id from pi_delegate"},
                "feedback": {"type": "string",
                             "description": "Concrete acceptance-check findings for the Pi orchestrator"},
            },
            "required": ["job_id", "feedback"],
        },
    },
    "pi_cancel": {
        "name": "pi_cancel",
        "description": (
            "Cancel a running bridge-created Pi job. Only jobs created by "
            "pi_delegate can be cancelled; arbitrary processes are never "
            "touched."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job id from pi_delegate"},
            },
            "required": ["job_id"],
        },
    },
}

_HANDLERS = {
    "pi_delegate": pi_delegate,
    "pi_list": pi_list,
    "pi_status": pi_status,
    "pi_feedback": pi_feedback,
    "pi_cancel": pi_cancel,
}


def register(ctx) -> None:
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="pi_bridge",
            schema=schema,
            handler=_safe(_HANDLERS[name]),
        )
