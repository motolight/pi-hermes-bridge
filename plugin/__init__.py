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
_WEBUI_MAX_ATTEMPTS = 1200   # ~1 h at the 3 s tick
_request_name = "webui_delivery.json"


def _bridge_home():
    from pathlib import Path
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


def _webui_may_host(ui_session_id):
    import sys
    # the gateway messaging runner imports tui_gateway.server too (its
    # hosted-room worker) and its delegation watcher terminally DROPS
    # completions whose parent session it cannot resolve: never inject there
    if "gateway.run" in sys.modules:
        return False
    if any(m in sys.modules for m in _BACKEND_MARKERS):
        return True
    return _webui_lease_pid(ui_session_id) == os.getpid()


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
    hermes = _webui_hermes()
    for req in _webui_scan():
        try:
            if not _webui_may_host(req.get("ui_session_id") or ""):
                continue
            if hermes is None:
                # inside a Hermes process whose internals are not importable
                # (odd install, partially initialized): visible, retried.
                claimed = _webui_claim(req)
                if claimed is not None:
                    _webui_release(claimed, "unsupported",
                                   reason="hermes-internal-unavailable")
                continue
            claimed = _webui_claim(req)
            if claimed is None:
                continue
            try:
                st = claimed.get("state")
                if st in ("pending", "inflight"):
                    if int(claimed.get("attempts") or 0) >= \
                            _WEBUI_MAX_ATTEMPTS:
                        _webui_release(
                            claimed, "abandoned",
                            reason="injection-attempts-exhausted")
                        _webui_ack_job(hermes, claimed, False,
                                       "injection-attempts-exhausted")
                        continue
                    injected = dict(claimed,
                                    attempts=int(claimed.get("attempts")
                                                 or 0) + 1)
                    _webui_inject(hermes, injected)
                    _webui_release(injected, "injected")
                    continue
                # st == "injected": read the durable ledger ack.
                row = _webui_ledger_state(hermes,
                                          _webui_delegation_id(claimed))
                if row is None:  # ledger row vanished -> re-inject
                    _webui_inject(hermes, claimed)
                    _webui_release(claimed, "injected")
                    continue
                delivery, claimed_now = row
                if delivery == "delivered":
                    _webui_release(claimed, "delivered", reason="ok")
                    _webui_ack_job(hermes, claimed, True, "ok")
                elif delivery == "dropped":
                    _webui_release(claimed, "abandoned",
                                   reason="native-delivery-exhausted")
                    _webui_ack_job(hermes, claimed, False,
                                   "native-delivery-exhausted")
                elif claimed_now:
                    # another consumer holds a live claim: put the request
                    # back where the next tick's scan can find it, or the
                    # ack would never be observed.
                    _webui_release(claimed, "injected")
                elif int(claimed.get("attempts") or 0) >= _WEBUI_MAX_ATTEMPTS:
                    _webui_release(claimed, "abandoned",
                                   reason="injection-attempts-exhausted")
                    _webui_ack_job(hermes, claimed, False,
                                   "injection-attempts-exhausted")
                else:  # pending, unclaimed -> safe to re-queue the event
                    claimed = dict(claimed,
                                   attempts=int(
                                       claimed.get("attempts") or 0) + 1)
                    _webui_inject(hermes, claimed)
                    _webui_release(claimed, "injected")
            except Exception as exc:
                try:
                    _webui_release(claimed, "pending",
                                   reason=f"{type(exc).__name__}")
                except Exception:
                    pass
        except Exception:
            pass  # the poller thread must never die


def _webui_poller_loop(stop_event):
    while not stop_event.wait(_WEBUI_POLL_SECONDS):
        try:
            _webui_poll_once()
        except Exception:
            pass


def _start_webui_poller():
    import threading
    global _webui_poller_state
    if _webui_poller_state is not None:
        return
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
