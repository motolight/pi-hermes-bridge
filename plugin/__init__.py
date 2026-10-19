"""pi-worker: Hermes user plugin exposing the durable pi-bridge as 5 tools.

Thin adapter only: every tool shells out (argv, no shell) to the standalone
`pi-bridge` CLI. The plugin never talks to pi directly and never selects Pi
subagents -- the Pi orchestrator decides that itself. No privileged
capabilities are declared or used.
"""
from __future__ import annotations

import json
import os
import re
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


# ---------------------------------------------------------------------------
# Task-integrity guard (v0.4.1)
# ---------------------------------------------------------------------------
# Hermes history compaction rewrites OLD tool-call arguments to end with a
# "...[truncated]" marker (agent/context_compressor.py); a model composing a
# pi_delegate task from compacted context can copy that marker verbatim.
# The bridge never truncates (submit takes the full stdin up to a hard cap
# and errors past it), but it must not launch a doomed job either: a task
# whose text ends with a compaction marker is REFUSED with an actionable
# error so the caller resends the full text.
_TRUNCATION_TAIL = re.compile(
    r"[\"\u00bb\u201d)\]]*\s*\.?\s*\[\s*truncated\s*]\s*"
    r"[\"\u00bb\u201d).,\]]*\s*$", re.IGNORECASE)


def _looks_compaction_truncated(task: str) -> bool:
    return bool(_TRUNCATION_TAIL.search(task or ""))


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
    if _looks_compaction_truncated(task):
        return json.dumps({
            "error": "task-text-truncated: the task text ends with a "
                     "history-compaction marker ('... [truncated]'), i.e. "
                     "it reached you already cut off. The bridge never "
                     "truncates tasks; do not submit a partial task -- "
                     "recompose it from the FULL original text (re-read the "
                     "user message or the source file if needed) and call "
                     "pi_delegate again with the complete task."})
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
            "the job ends, the bridge records the outcome and delivers it "
            "back along the current session's recorded origin itself (for "
            "web sessions as a native Hermes background-completion event "
            "that starts an acceptance turn in this session), and wakes a "
            "Hermes acceptance run by itself -- you do not have to poll, "
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


# ---------------------------------------------------------------------------
# V1.5 webui completion poller (in-process, native Hermes delivery)
# ---------------------------------------------------------------------------
# Why: delivering a finished Pi job to a live webui session by running
# `hermes --resume <sid> chat -q ...` in a SECOND process raced the backend's
# session lease and in-memory history (orphaned user-like messages, killed
# acceptance turns).  The runner now only spools a durable request next to
# the job (pi_bridge/webui_event.py); THIS thread -- already running inside
# the Hermes process that owns the session -- injects it into Hermes' own
# background-completion pipeline (async-delegation event + ledger +
# completion queue), whose per-session notification poller starts a real
# acceptance turn at the session's idle boundary, with durable at-most-once
# claims and restart replay.  No user-role message, no lease contention, no
# reload requirement.  pi_status stays the durable source of truth.
#
# The spool file logic below is a deliberate stdlib-only mirror of
# pi_bridge/webui_event.py (the plugin is copied into ~/.hermes/plugins and
# cannot import the bridge venv); tests/test_webui_event_parity.py pins the
# two implementations against shared fixtures.
_WEBUI_POLL_SECONDS = 3.0
_WEBUI_INFLIGHT_STALE_S = 120.0
_PLUGIN_VERSION = "0.4.2"    # keep in sync with pyproject.toml project.version
_WEBUI_PROTOCOL = "V1.5"
_DEFAULT_MAX_ATTEMPTS = 1200   # ~1 h at the 3 s tick


def _env_int(name, default):
    """Positive-int env override; anything unparsable or <= 0 is garbage."""
    try:
        val = int((os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


_WEBUI_MAX_ATTEMPTS = _env_int("PI_BRIDGE_MAX_INJECTION_ATTEMPTS",
                               _DEFAULT_MAX_ATTEMPTS)
_request_name = "webui_delivery.json"


_home_pin = None  # captured at poller-thread start (see _start_webui_poller)


def _bridge_home():
    from pathlib import Path
    if _home_pin is not None:
        return _home_pin
    home = os.environ.get("PI_BRIDGE_HOME")
    if home:
        return Path(home).expanduser().absolute()
    return Path.home() / ".local" / "state" / "pi-bridge"


def _webui_scan():
    """Actionable requests: pending/injected/unsupported, and crashed
    claimers' inflight copies older than _WEBUI_INFLIGHT_STALE_S."""
    import json as _json
    import time as _time
    jobs = _bridge_home() / "jobs"
    out = []
    try:
        entries = sorted(jobs.iterdir(), reverse=True)
    except OSError:
        return out
    for d in entries:
        try:
            req = _json.loads((d / _request_name).read_text("utf-8"))
        except (OSError, ValueError):
            req = None
        if isinstance(req, dict) and req.get("job_id"):
            if req.get("state") in ("pending", "injected", "unsupported"):
                req["_dir"] = str(d)
                out.append(req)
            continue
        # canonical gone: a crashed claimer's copy, if stale, is reclaimable
        try:
            inflights = sorted(
                d.glob(_request_name + ".inflight.*"),
                key=lambda p: p.stat().st_mtime)
        except OSError:
            continue
        for p in reversed(inflights):        # newest first
            try:
                stale = _json.loads(p.read_text("utf-8"))
                age = _time.time() - p.stat().st_mtime
            except (OSError, ValueError):
                break
            if age > _WEBUI_INFLIGHT_STALE_S and isinstance(stale, dict) \
                    and stale.get("job_id"):
                stale["_dir"] = str(d)
                stale["_inflight_path"] = str(p)
                out.append(stale)
            break                            # a live claim hides the rest
    return out


def _webui_claim(req):
    """One-shot ownership: rename the live file (canonical, or the stale
    inflight copy found by the scan) to a pid-tagged copy."""
    import json as _json
    src = req.get("_inflight_path") or \
        f"{req['_dir']}/{_request_name}"
    dst = f"{req['_dir']}/{_request_name}.inflight.{os.getpid()}"
    if src == dst:
        return None  # re-claiming this process's own live claim: no-op
    try:
        os.rename(src, dst)
        os.utime(dst, None)  # a fresh claim is never "stale"
        raw = _json.loads(open(dst, encoding="utf-8").read())
    except (OSError, ValueError):
        return None
    raw["_dir"] = req["_dir"]
    raw["_inflight_path"] = dst
    return raw


def _webui_release(req, state_value, reason=None):
    """Canonical file gets the new state; the inflight copy is dropped."""
    import json as _json
    import time as _time
    from datetime import datetime, timezone
    from pathlib import Path
    d = Path(req["_dir"])
    keep = {k: v for k, v in req.items()
            if not k.startswith("_") and k != "reason"}
    keep["state"] = state_value
    if reason is not None:
        keep["reason"] = reason
    keep["updated_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    tmp = d / (_request_name + f".tmp.{os.getpid()}")
    tmp.write_text(_json.dumps(keep, ensure_ascii=False), "utf-8")
    os.replace(tmp, d / _request_name)
    inflight = req.get("_inflight_path")
    if inflight:
        try:
            os.unlink(inflight)
        except OSError:
            pass
    # housekeeping: garbage inflight copies of crashed claimers, stale by
    # age, and crashed atomic-write tmp files must not pile up.
    try:
        now = _time.time()
        for p in list(d.glob(_request_name + ".inflight.*")) \
                + list(d.glob(_request_name + ".tmp.*")):
            try:
                if now - p.stat().st_mtime > _WEBUI_INFLIGHT_STALE_S:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass
    return keep


def _webui_hermes():
    """(ledger_tx, queue) from Hermes internals, or None outside Hermes."""
    try:
        from tools.async_delegation import _DB_LOCK, _transaction
        from tools.process_registry import process_registry
    except Exception:
        return None
    return _DB_LOCK, _transaction, process_registry


def _webui_delegation_id(req):
    return f"pi-bridge-{req['job_id']}-t{int(req.get('turn') or 0)}"


def _webui_build_event(req):
    import calendar
    import time as _time
    jid = str(req["job_id"])
    turn = int(req.get("turn") or 0)
    status = req.get("status") or "completed"
    sid = str(req.get("ui_session_id") or "")
    now = _time.time()
    try:
        # created_at is Zulu: parse as UTC, never as local time.
        created = calendar.timegm(_time.strptime(
            req.get("created_at") or "", "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        created = now
    goal = str(req.get("goal") or "").strip() \
        or f"Pi job {jid} (turn {turn})"
    return {
        "type": "async_delegation",
        "delegation_id": _webui_delegation_id(req),
        "session_key": sid,
        "origin_ui_session_id": sid,
        "origin_session_id": "",
        "parent_session_id": sid,
        "goal": goal,
        "context": None, "toolsets": None, "role": None, "model": None,
        "is_batch": False,
        "status": status,
        "summary": req.get("text") or "",
        "error": None if status == "completed" else
                 (req.get("error") or "the Pi turn failed"),
        "dispatched_at": created,
        "completed_at": now,
    }


def _webui_ledger_state(hermes, delegation_id):
    """(delivery_state, claimed_now) from the durable ledger; None if absent."""
    _DB_LOCK, _transaction, _registry = hermes
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT delivery_state, delivery_claim FROM async_delegations "
            "WHERE delegation_id=?", (delegation_id,)).fetchone()
    return (row[0], bool(row[1])) if row else None


def _webui_inject(hermes, req):
    """Durable ledger row (INSERT OR IGNORE) + native queue event."""
    import json as _json
    _DB_LOCK, _transaction, registry = hermes
    evt = _webui_build_event(req)
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, completed_at,
                updated_at, event_json, delivery_state)
               VALUES (?,?,?,?,?,?,?,?,?,'pending')""",
            (str(evt["delegation_id"]), str(evt.get("session_key") or ""),
             str(evt.get("origin_ui_session_id") or ""),
             evt.get("parent_session_id"), "completed",
             float(evt["dispatched_at"]), float(evt["completed_at"]),
             float(evt["completed_at"]), _json.dumps(evt,
                                                     ensure_ascii=False)))
    registry.completion_queue.put(evt)
    return evt


def _webui_lease_pid(ui_session_id):
    """pid of the live lease holder of a session, or None."""
    import json as _json
    from pathlib import Path
    env = (os.environ.get("HERMES_HOME") or "").strip()
    home = Path(env).expanduser() if env else Path.home() / ".hermes"
    path = home / "runtime" / "active_sessions.json"
    try:
        data = _json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    for e in (data.get("entries") or []) if isinstance(data, dict) else []:
        if isinstance(e, dict) and str(e.get("session_id") or "") == \
                str(ui_session_id):
            try:
                return int(e.get("pid"))
            except (TypeError, ValueError):
                return None
    return None


_BACKEND_MARKERS = ("api.background_process", "tui_gateway.server")

# ---------------------------------------------------------------------------
# process-role host gate (v0.4.2, incident pb-20261019T082242Z-8d9299)
# ---------------------------------------------------------------------------
# The v0.4.1 gate vetoed every process with "gateway.run" in sys.modules.
# But the REAL WebUI backend (hermes-webui server.py -- the legitimate
# native host) lazily imports gateway.run in-process (agent/relay_runtime.py
# via context compression) AFTER it has already hosted completions, which
# permanently poisoned its poller.  The decision therefore starts from
# PERSISTENT process identity -- argv, environment and the __main__ file --
# which a lazy import cannot forge; loaded modules are only consulted for
# processes whose persistent identity is unknown.  Parity with
# pi_bridge/webui_event.host_decision is pinned by
# tests/test_webui_event_parity.py.
_WEBUI_IDENTITY_ENV = ("HERMES_WEBUI_STATE_DIR", "HERMES_WEBUI_AGENT_DIR")


def _norm_path(p) -> str:
    return str(p or "").replace("\\", "/")


def _path_ends(p, suffix) -> bool:
    """True when the normalized path ends with `suffix` on a path-element
    boundary ('x/gateway/run.py' yes, 'x/mygateway/run.py' no)."""
    p = _norm_path(p)
    return p == suffix or p.endswith("/" + suffix)


def _argv_module(argv, name) -> bool:
    """True when argv runs module `name` (`python -m name ...`)."""
    argv = list(argv or [])
    for i in range(len(argv) - 1):
        if argv[i] == "-m" and argv[i + 1] == name:
            return True
    return False


def _main_file_default() -> str:
    import sys
    mf = getattr(sys.modules.get("__main__"), "__file__", None) \
        or (sys.argv[0] if sys.argv else "")
    return str(mf or "")


def _webui_process_role(argv=None, environ=None, main_file=None):
    """'gateway' | 'webui' | 'tui_backend' | 'unknown' from PERSISTENT
    process identity only (argv, environment, the __main__ file -- all
    injectable for tests).  Gateway signals are checked FIRST so a gateway
    runner is vetoed even before it lazily imports gateway.run; webui
    markers (HERMES_WEBUI_* env, hermes-webui/server.py|bootstrap.py) come
    next, then the tui/desktop runner."""
    import sys
    if argv is None:
        argv = sys.argv
    if environ is None:
        environ = os.environ
    if main_file is None:
        main_file = _main_file_default()
    argv = [str(a) for a in (argv or [])]
    mf = _norm_path(main_file)
    is_hermes_cli = _argv_module(argv, "hermes_cli.main") \
        or (argv and os.path.basename(argv[0]) == "hermes")
    if (is_hermes_cli and "gateway" in argv[1:]) \
            or "gateway.run" in argv or _path_ends(mf, "gateway/run.py"):
        return "gateway"
    if any(str(environ.get(k) or "").strip() for k in _WEBUI_IDENTITY_ENV) \
            or _path_ends(mf, "hermes-webui/server.py") \
            or _path_ends(mf, "hermes-webui/bootstrap.py"):
        return "webui"
    if _argv_module(argv, "tui_gateway") \
            or _argv_module(argv, "tui_gateway.entry") \
            or _path_ends(mf, "tui_gateway/entry.py"):
        return "tui_backend"
    return "unknown"


def _webui_record_gate(ok, reason):
    """Remember the latest gate outcome for the health record; a reason
    TRANSITION logs exactly one line (never one per tick)."""
    global _gate_reason_seen
    if _health is not None:
        _health["may_host"] = bool(ok)
        _health["last_gate_reason"] = reason
        if reason != _gate_reason_seen:
            _gate_reason_seen = reason
            _logger().info(f"gate pid={os.getpid()} host={bool(ok)} "
                           f"reason={reason}")
    return bool(ok), reason


def _webui_host_decision(ui_session_id, *, argv=None, environ=None,
                         main_file=None):
    """(may_inject, reason) for THIS process:
    1. an argv/env-identified gateway runner NEVER injects (its delegation
       watcher terminally DROPS completions whose parent it cannot resolve)
       regardless of which modules it happens to have loaded -- and
       regardless of a live session lease;
    2. a positive webui identity (webui role OR api.background_process, a
       hermes-webui-exclusive module) always hosts -- this MUST win over a
       LATE incidental import of gateway.run (the production regression);
    3. the tui/desktop backend role hosts;
    4. unknown role: a loaded gateway.run still vetoes (v0.4.1 behaviour),
       then the legacy backend markers allow hosting;
    5. finally the durable lease fallback: this process owns the session."""
    import sys
    role = _webui_process_role(argv=argv, environ=environ, main_file=main_file)
    if role == "gateway":
        return _webui_record_gate(False, "gateway-runner")
    if role == "webui" or "api.background_process" in sys.modules:
        return _webui_record_gate(True, "webui-positive")
    if role == "tui_backend":
        return _webui_record_gate(True, "tui-backend")
    if "gateway.run" in sys.modules:
        return _webui_record_gate(False, "gateway-run-loaded")
    if any(m in sys.modules for m in _BACKEND_MARKERS):
        return _webui_record_gate(True, "backend-marker")
    if _webui_lease_pid(ui_session_id) == os.getpid():
        return _webui_record_gate(True, "lease-self")
    return _webui_record_gate(False, "no-host")


def _webui_may_host(ui_session_id):
    return _webui_host_decision(ui_session_id)[0]


# ---------------------------------------------------------------------------
# poller observability (v0.4.2): durable health record + rate-limited logs
# ---------------------------------------------------------------------------
# One JSON file per poller process at $PI_BRIDGE_HOME/health/poller-<pid>.json
# (dir 0700, file 0600, atomic tmp+rename), read back by `pi-bridge health`
# (pi_bridge/health.py).  Contents are pure bookkeeping: NEVER task text,
# result text, ui_session_id or secrets.  Written on startup, when anything
# meaningful changes and at least every _HEALTH_HEARTBEAT_S -- never on every
# 3 s tick.  Logs go to logging.getLogger("pi_worker"): one start line, one
# line per gate-reason transition, one line per claim/inject/delivered/
# abandoned event (rare by nature) and unexpected exceptions rate-limited to
# one line per identical message per _LOG_RETRY_S.  Idle ticks log nothing.
_HEALTH_HEARTBEAT_S = 60.0
_LOG_RETRY_S = 300.0
# fields that change on every tick and are therefore NOT a "meaningful
# change" for the write policy (otherwise the 3 s tick would rewrite the
# record forever):
_HEALTH_TS_FIELDS = ("last_tick_at", "last_scan_at")
# ticks/scans always; gate_rejected/pending_refused_count keep growing while
# some OTHER process holds a pending request this process must not host (the
# incident's steady state!) -- they are observability, not change signals.
_HEALTH_FREE_COUNTERS = ("ticks", "scans", "gate_rejected")
_HEALTH_TMP_STALE_S = 3600.0

_health = None               # live record dict (None until the first tick)
_health_written_norm = None  # normalized JSON of the record last on disk
_health_last_write_ts = 0.0
_health_write_count = 0      # real file writes so far (tests/diagnostics)
_gate_reason_seen = None
_log_seen = {}               # message -> last logged epoch (rate limit)


def _logger():
    import logging
    return logging.getLogger("pi_worker")


def _health_now() -> float:
    import time
    return time.time()


def _health_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_limited(msg):
    """One WARNING line per identical message per _LOG_RETRY_S."""
    import logging
    now = _health_now()
    last = _log_seen.get(msg)
    if last is not None and now - last < _LOG_RETRY_S:
        return
    _log_seen[msg] = now
    _logger().log(logging.WARNING, msg)


def _health_new(pid):
    return {
        "pid": pid,
        "plugin_version": _PLUGIN_VERSION,
        "protocol": _WEBUI_PROTOCOL,
        "role": "unknown",
        "may_host": False,
        "signals": {"gateway_run_loaded": False,
                    "api_background_process_loaded": False,
                    "tui_gateway_server_loaded": False},
        "started_at": _health_iso(),
        "last_tick_at": "",
        "last_scan_at": "",
        "last_pending_count": 0,
        "last_gate_reason": "",
        "pending_refused_count": 0,
        "last": {"injected_job": None, "delivered_job": None,
                 "abandoned_job": None, "error": None},
        "counters": {"ticks": 0, "scans": 0, "injected": 0, "delivered": 0,
                     "abandoned": 0, "gate_rejected": 0},
    }


def _health_first_tick(pid):
    """Lazy init: a health record exists from the first tick even when the
    thread was never started (tests drive _webui_poll_once directly)."""
    global _health
    _health = _health_new(pid)
    _health["counters"]["ticks"] = 1
    _health["role"] = _webui_process_role()
    _webui_host_decision("")      # seed the gate state (no session yet)
    _health["last_tick_at"] = _health_iso()
    _logger().info(f"poller started pid={pid} role={_health['role']}")
    _health_write(force=True)
    return _health


def _health_scan(reqs):
    import sys
    _health["counters"]["scans"] += 1
    _health["last_scan_at"] = _health_iso()
    _health["last_pending_count"] = sum(
        1 for r in reqs if r.get("state") in ("pending", "inflight"))
    sig = _health["signals"]
    sig["gateway_run_loaded"] = "gateway.run" in sys.modules
    sig["api_background_process_loaded"] = "api.background_process" \
        in sys.modules
    sig["tui_gateway_server_loaded"] = "tui_gateway.server" in sys.modules


def _health_gate_refused(state_value):
    if _health is None:
        return
    _health["counters"]["gate_rejected"] += 1
    if state_value in ("pending", "inflight"):
        _health["pending_refused_count"] += 1


def _health_event(kind, job_id, reason=None):
    """counters/last + one INFO line per delivery-lifecycle event (these are
    rare by nature -- claims log separately in the tick)."""
    if _health is None:
        return
    c = _health["counters"]
    if kind == "injected":
        _health["last"]["injected_job"] = str(job_id)
        c["injected"] += 1
    elif kind == "delivered":
        _health["last"]["delivered_job"] = str(job_id)
        c["delivered"] += 1
    elif kind == "abandoned":
        _health["last"]["abandoned_job"] = str(job_id)
        c["abandoned"] += 1
    _logger().info(f"{kind} job={job_id}"
                   + (f" reason={reason}" if reason else ""))


def _health_error(exc):
    if _health is None:
        return
    _health["last"]["error"] = {"type": type(exc).__name__,
                                "at": _health_iso()}


def _health_write(force=False):
    """Atomic 0600 health record; only on startup, meaningful change
    (counters other than ticks/scans, gate, pending count, last_*) or the
    60 s heartbeat -- never on every 3 s tick."""
    global _health_written_norm, _health_last_write_ts, _health_write_count
    import json as _json
    from pathlib import Path  # noqa: F401  (style parity with the spool code)
    if _health is None:
        return
    body = {k: v for k, v in _health.items() if k not in _HEALTH_TS_FIELDS}
    body.pop("pending_refused_count", None)
    body["counters"] = {k: v for k, v in (_health["counters"].items())
                        if k not in _HEALTH_FREE_COUNTERS}
    last = dict(_health.get("last") or {})
    if isinstance(last.get("error"), dict):
        # the error TYPE is a meaningful change; its timestamp is not (a
        # repeatedly failing injection would otherwise rewrite every tick)
        last["error"] = {"type": last["error"].get("type")}
    body["last"] = last
    norm = _json.dumps(body, ensure_ascii=False, sort_keys=True)
    now = _health_now()
    if not force and norm == _health_written_norm \
            and now - _health_last_write_ts < _HEALTH_HEARTBEAT_S:
        return
    try:
        d = _bridge_home() / "health"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(d, 0o700)
        raw = _json.dumps(_health, ensure_ascii=False, sort_keys=True)
        final = d / f"poller-{_health['pid']}.json"
        tmp = d / f".poller-{_health['pid']}.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(raw)
        os.chmod(tmp, 0o600)
        os.replace(tmp, final)
        _health_written_norm = norm
        _health_last_write_ts = now
        _health_write_count += 1
        # a crashed writer leaves .poller-*.json.tmp behind; reap old ones
        # opportunistically (bounded dir, cheap glob).
        for p in d.glob(".poller-*.json.tmp"):
            try:
                if now - p.stat().st_mtime > _HEALTH_TMP_STALE_S:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass  # observability must never break the poller


def _webui_ack_job(hermes, req, ok, reason):
    """Mirror the ack into the job's delivery field (never status)."""
    import fcntl
    import json as _json
    from datetime import datetime, timezone
    from pathlib import Path
    jd = Path(req["_dir"])
    try:
        lf = open(jd / ".lock", "a+")
    except OSError:
        return
    try:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # the runner is mid-write: the next tick retries
        try:
            job = _json.loads((jd / "job.json").read_text("utf-8"))
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            job["delivery"] = {
                "attempted": True, "ok": bool(ok),
                "channel": f"webui:{req.get('ui_session_id', '')}",
                "kind": "webui-event", "reason": reason, "error": None,
                "turn": len(job.get("turns") or []), "at": now}
            tmp = jd / f"job.json.tmp.{os.getpid()}"
            tmp.write_text(_json.dumps(job, ensure_ascii=False), "utf-8")
            os.replace(tmp, jd / "job.json")
        except (OSError, ValueError):
            pass
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    finally:
        lf.close()


def _webui_poll_once():
    if _health is None:
        _health_first_tick(os.getpid())
    else:
        _health["counters"]["ticks"] += 1
        _health["last_tick_at"] = _health_iso()
    try:
        hermes = _webui_hermes()
        reqs = _webui_scan()
        _health_scan(reqs)
        for req in reqs:
            try:
                if not _webui_may_host(req.get("ui_session_id") or ""):
                    _health_gate_refused(req.get("state"))
                    continue
                if hermes is None:
                    # inside a Hermes process whose internals are not
                    # importable (odd install, partially initialized):
                    # visible, retried.
                    claimed = _webui_claim(req)
                    if claimed is not None:
                        _logger().info(f"claim job={claimed.get('job_id')}")
                        _webui_release(claimed, "unsupported",
                                       reason="hermes-internal-unavailable")
                    continue
                claimed = _webui_claim(req)
                if claimed is None:
                    continue
                _logger().info(f"claim job={claimed.get('job_id')}")
                try:
                    st = claimed.get("state")
                    jid = claimed.get("job_id")
                    if st in ("pending", "inflight"):
                        if int(claimed.get("attempts") or 0) >= \
                                _WEBUI_MAX_ATTEMPTS:
                            _webui_release(
                                claimed, "abandoned",
                                reason="injection-attempts-exhausted")
                            _webui_ack_job(hermes, claimed, False,
                                           "injection-attempts-exhausted")
                            _health_event("abandoned", jid,
                                          "injection-attempts-exhausted")
                            continue
                        injected = dict(
                            claimed,
                            attempts=int(claimed.get("attempts") or 0) + 1)
                        _webui_inject(hermes, injected)
                        _webui_release(injected, "injected")
                        _health_event("injected", jid)
                        continue
                    # st == "injected": read the durable ledger ack.
                    row = _webui_ledger_state(
                        hermes, _webui_delegation_id(claimed))
                    if row is None:  # ledger row vanished -> re-inject
                        claimed = dict(
                            claimed,
                            attempts=int(claimed.get("attempts") or 0) + 1)
                        _webui_inject(hermes, claimed)
                        _webui_release(claimed, "injected")
                        _health_event("injected", jid)
                        continue
                    delivery, claimed_now = row
                    if delivery == "delivered":
                        _webui_release(claimed, "delivered", reason="ok")
                        _webui_ack_job(hermes, claimed, True, "ok")
                        _health_event("delivered", jid)
                    elif delivery == "dropped":
                        _webui_release(claimed, "abandoned",
                                       reason="native-delivery-exhausted")
                        _webui_ack_job(hermes, claimed, False,
                                       "native-delivery-exhausted")
                        _health_event("abandoned", jid,
                                      "native-delivery-exhausted")
                    elif claimed_now:
                        # another consumer holds a live claim: put the
                        # request back where the next tick's scan can find
                        # it, or the ack would never be observed.
                        _webui_release(claimed, "injected")
                    elif int(claimed.get("attempts") or 0) >= \
                            _WEBUI_MAX_ATTEMPTS:
                        _webui_release(claimed, "abandoned",
                                       reason="injection-attempts-exhausted")
                        _webui_ack_job(hermes, claimed, False,
                                       "injection-attempts-exhausted")
                        _health_event("abandoned", jid,
                                      "injection-attempts-exhausted")
                    else:  # pending, unclaimed -> safe to re-queue the event
                        claimed = dict(
                            claimed,
                            attempts=int(claimed.get("attempts") or 0) + 1)
                        _webui_inject(hermes, claimed)
                        _webui_release(claimed, "injected")
                        _health_event("injected", jid)
                except Exception as exc:
                    # EVERY real injection attempt burns the durable attempt
                    # budget -- also the ones that raise, or a permanently
                    # failing injection would retry forever (v0.4.2 fix).
                    retry = dict(claimed,
                                 attempts=int(claimed.get("attempts") or 0)
                                 + 1)
                    try:
                        _webui_release(retry, "pending",
                                       reason=f"{type(exc).__name__}")
                    except Exception:
                        pass
                    _health_error(exc)
                    _log_limited(f"inject failed job={jid}: "
                                 f"{type(exc).__name__}")
            except Exception as exc:
                _health_error(exc)
                _log_limited(f"poll item failed: {type(exc).__name__}")
                # the poller thread must never die
    except Exception as exc:
        _health_error(exc)
        _log_limited(f"poll tick failed: {type(exc).__name__}")
    finally:
        _health_write()


def _webui_poller_loop(stop_event):
    while not stop_event.wait(_WEBUI_POLL_SECONDS):
        try:
            _webui_poll_once()
        except Exception:
            pass


def _start_webui_poller():
    import threading
    global _webui_poller_state, _home_pin
    if _webui_poller_state is not None:
        return
    # The thread outlives any caller's environment (in tests the fixture env
    # is torn down while this daemon thread keeps ticking).  Pin the bridge
    # home NOW so every later tick -- scans AND health writes -- stays in
    # the tree the process was started against.  Production env is stable,
    # so the pin is identical to the env lookup there.
    _home_pin = _bridge_home()
    stop = threading.Event()
    t = threading.Thread(target=_webui_poller_loop, args=(stop,),
                         name="pi-worker-webui-poller", daemon=True)
    _webui_poller_state = (t, stop)
    t.start()


_webui_poller_state = None


def register(ctx) -> None:
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="pi_bridge",
            schema=schema,
            handler=_safe(_HANDLERS[name]),
        )
    try:  # never let the optional poller break tool registration
        _start_webui_poller()
    except Exception:
        pass
