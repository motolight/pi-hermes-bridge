"""V1.5 webui delivery: a durable spool, consumed by the Hermes plugin.

Why (see docs/WEBUI_COMPLETION_EVENT_SPEC.md)
---------------------------------------------
v0.4.0 handed a finished Pi result back to a webui origin by running
`hermes --resume <ui_session_id> chat -q <text> -Q --source tool` -- a second
agent turn in a second process, on a session the live WebUI backend owns.
That races the backend's in-memory history (the CLI's user-role message is
persisted while its answer can be killed mid-model-call), which produced
orphaned user-like messages and stalled acceptances.  A user-like chat turn
is not a transport.

The fix splits delivery in two:
* the runner only RECORDS a durable delivery request (this module's spool,
  written atomically inside the job directory, `webui_delivery.json`);
* the bridge's Hermes plugin -- code that already runs INSIDE the Hermes
  agent process -- drains the spool in-process and injects the completion
  into Hermes' own native background-completion pipeline (the async
  delegation event/ledger/queue), whose per-session notification poller
  starts a real acceptance turn at the session's idle boundary.

This module is stdlib-only and side-effect-free apart from its own files:
importable by the runner, the CLI, the plugin and tests alike.  It never
imports Hermes internals and never talks to a queue: injection lives behind
the adapter the plugin supplies (`injector` callbacks).

Request lifecycle (status field):
    pending -> (claim) inflight -> injected -> delivered     (ok)
                                  -> abandoned / unsupported (terminal-ish)
A never-claimed request simply stays pending: nothing is lost, pi_status
remains the durable source of truth, and every transition is one file.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import state

REQUEST_NAME = "webui_delivery.json"
STATUSES = ("pending", "inflight", "injected", "delivered", "abandoned",
            "unsupported")
MAX_INFLIGHT_AGE_S = 120.0        # a crashed claimer must not wedge a request


def _max_attempts_default() -> int:
    """PI_BRIDGE_MAX_INJECTION_ATTEMPTS (> 0) or the 1200 default (~1 h at
    the plugin's 3 s tick); unparsable or non-positive values are garbage
    and fall back to the default.  Read once at import; the module constant
    MAX_INJECTION_ATTEMPTS stays patchable for tests."""
    try:
        val = int((os.environ.get("PI_BRIDGE_MAX_INJECTION_ATTEMPTS")
                   or "").strip())
    except (TypeError, ValueError):
        return 1200
    return val if val > 0 else 1200


MAX_INJECTION_ATTEMPTS = _max_attempts_default()


def _now_epoch() -> float:
    return time.time()


def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def request_path(job_id: str) -> Path:
    return state.job_dir(job_id) / REQUEST_NAME


def write_request(job_id: str, *, ui_session_id: str, turn: int, text: str,
                  status: str = "completed", error: str | None = None,
                  goal: str = "") -> dict:
    """Record (or supersede) the webui delivery request of one finished turn.

    One active request per job by construction: a later turn overwrites the
    file, and the plugin delivers exactly one completion event per (job,
    turn) because the delegation_id is derived from both.  `goal` is the
    compact UI title of the completion card.
    """
    req = {
        "job_id": job_id,
        "ui_session_id": ui_session_id,
        "turn": int(turn),
        "text": text,
        "goal": goal,
        "status": status,          # job outcome: completed | failed
        "error": error,
        "state": "pending",        # delivery state machine above
        "attempts": 0,
        "created_at": state.now_iso(),
        "updated_at": state.now_iso(),
    }
    state.write_text_atomic(request_path(job_id),
                            json.dumps(req, ensure_ascii=False))
    return req


def read_request(job_dir: Path) -> dict | None:
    try:
        raw = json.loads((job_dir / REQUEST_NAME).read_text("utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError,
            UnicodeDecodeError):
        return None
    if not isinstance(raw, dict) or not raw.get("job_id"):
        return None
    raw["_dir"] = str(job_dir)
    return raw


def find_inflight(job_dir: Path) -> dict | None:
    """The newest crashed claimer's inflight copy, if older than
    MAX_INFLIGHT_AGE_S (a live claim is never returned)."""
    try:
        names = sorted(job_dir.glob(REQUEST_NAME + ".inflight.*"),
                       key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    for p in reversed(names):  # newest first
        try:
            raw = json.loads(p.read_text("utf-8"))
            age = _now_epoch() - p.stat().st_mtime
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            continue
        if age > MAX_INFLIGHT_AGE_S and isinstance(raw, dict):
            raw["_inflight_path"] = str(p)
            return raw
        return None
    return None


def iter_requests() -> list[dict]:
    """All jobs' delivery requests that are still actionable (newest job
    first).  'pending' (never injected), stale 'inflight' (stolen back), and
    'injected' (awaiting the ledger ack) are actionable; the rest are done."""
    out: list[dict] = []
    try:
        entries = sorted(state.jobs_root().iterdir(), reverse=True)
    except OSError:
        return out
    for d in entries:
        if not (d.is_dir() and (d / "job.json").exists()):
            continue
        req = read_request(d)
        if req is None:
            stale = find_inflight(d)
            if stale is not None:
                stale["_dir"] = str(d)
                out.append(stale)
            continue
        if req.get("state") in ("pending", "injected", "unsupported"):
            out.append(req)
    return out


def claim(req: dict) -> dict | None:
    """Atomically take ownership of one delivery attempt (pid-tagged rename).

    Returns the inflight copy to work on, or None if someone else holds it.
    'injected' requests (awaiting ack) are re-claimed through the inflight
    rename as well: exactly one poller polls the ledger per request.
    """
    d = Path(req["_dir"])
    src = Path(req["_inflight_path"]) if req.get("_inflight_path") \
        else d / REQUEST_NAME
    dst = d / (REQUEST_NAME + f".inflight.{os.getpid()}")
    if src == dst:
        return None  # re-claiming this process's own live claim: no-op
    try:
        os.rename(src, dst)
        os.utime(dst, None)  # a fresh claim is never "stale"
    except OSError:
        return None
    try:
        raw = json.loads(dst.read_text("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    raw["_dir"] = str(d)
    raw["_inflight_path"] = str(dst)
    return raw


def release(req: dict, state_value: str, reason: str | None = None) -> dict:
    """Write the final/updated request and drop the inflight copy.

    Transition is: update canonical file, then unlink inflight -- a crash
    between the two still converges (the canonical file wins; the inflight
    copy becomes stale garbage after MAX_INFLIGHT_AGE_S and is ignored).
    """
    d = Path(req["_dir"])
    keep = {k: v for k, v in req.items()
            if k not in ("_dir", "_inflight_path", "reason")}
    keep["state"] = state_value
    if reason is not None:
        keep["reason"] = reason
    keep["updated_at"] = state.now_iso()
    state.write_text_atomic(d / REQUEST_NAME, json.dumps(keep,
                                                         ensure_ascii=False))
    inflight = req.get("_inflight_path")
    if inflight:
        try:
            Path(inflight).unlink()
        except OSError:
            pass
    # housekeeping: garbage inflight copies from crashed claimers, stale by
    # age, must not pile up in the job directory (and crashed atomic writes
    # leave tmp files behind that no pattern may ever read).
    try:
        now = _now_epoch()
        for p in list(d.glob(REQUEST_NAME + ".inflight.*")) \
                + list(d.glob(REQUEST_NAME + ".tmp.*")):
            try:
                if now - p.stat().st_mtime > MAX_INFLIGHT_AGE_S:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass
    return keep


def bump_attempt(req: dict) -> dict:
    req = dict(req)
    req["attempts"] = int(req.get("attempts") or 0) + 1
    return req


def attempts_exhausted(req: dict) -> bool:
    return int(req.get("attempts") or 0) >= MAX_INJECTION_ATTEMPTS


# ---------------------------------------------------------------------------
# the native completion event (async-delegation shape)
# ---------------------------------------------------------------------------

def delegation_id(job_id: str, turn: int) -> str:
    """Stable per (job, turn): the ledger row / dedup identity.  Re-injecting
    the same completion can never start a second acceptance turn because the
    ledger claim is keyed on exactly this id."""
    return f"pi-bridge-{job_id}-t{turn}"


def build_event(req: dict, *, task_preview: str = "") -> dict:
    """The completion event for Hermes' native completion queue.

    Field names/shapes mirror tools/async_delegation's own events so the
    backend's notification poller routes it by ``origin_ui_session_id``,
    claims it durably via ``delegation_id`` and renders it with the standard
    ``async_delegation_complete`` display (never a user-role message).
    """
    job_id = str(req["job_id"])
    turn = int(req.get("turn") or 0)
    status = req.get("status") or "completed"
    now = _now_epoch()
    if status == "completed":
        summary, error = req.get("text") or "", None
    else:
        summary, error = req.get("text") or "", (req.get("error")
                                                 or "the Pi turn failed")
    sid = str(req.get("ui_session_id") or "")
    goal = str(req.get("goal") or "").strip() \
        or (f"Pi job {job_id} (turn {turn}): {task_preview[:120]}"
            if task_preview else f"Pi job {job_id} (turn {turn})")
    return {
        "type": "async_delegation",
        "delegation_id": delegation_id(job_id, turn),
        "session_key": sid,
        "origin_ui_session_id": sid,
        "origin_session_id": "",
        "parent_session_id": sid,
        "goal": goal,
        "context": None,
        "toolsets": None,
        "role": None,
        "model": None,
        "is_batch": False,
        "status": status,
        "summary": summary,
        "error": error,
        "dispatched_at": _epoch(req.get("created_at")) or now,
        "completed_at": now,
    }


def ledger_row(evt: dict) -> tuple:
    """INSERT parameters for the durable async_delegations ledger row.

    Column order (Hermes state.db, schema of tools/async_delegation):
    delegation_id, origin_session, origin_ui_session_id, parent_session_id,
    state, dispatched_at, completed_at, updated_at, event_json.
    state='completed' + delivery_state default 'pending' is exactly what
    Hermes' own restart replay (`restore_undelivered_completions`) consumes.
    """
    return (
        str(evt["delegation_id"]),
        str(evt.get("session_key") or ""),
        str(evt.get("origin_ui_session_id") or ""),
        evt.get("parent_session_id"),
        "completed",
        float(evt["dispatched_at"]),
        float(evt["completed_at"]),
        float(evt["completed_at"]),
        json.dumps(evt, ensure_ascii=False),
    )


# ---------------------------------------------------------------------------
# where may a completion event legitimately be injected?
# ---------------------------------------------------------------------------

def _hermes_home(hermes_home: Path | None = None) -> Path:
    if hermes_home is not None:
        return Path(hermes_home)
    env = (os.environ.get("HERMES_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def lease_owner(ui_session_id: str, hermes_home: Path | None = None):
    """(pid, surface) of the live lease holder of a session, or None."""
    path = _hermes_home(hermes_home) / "runtime" / "active_sessions.json"
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None
    for e in entries:
        if isinstance(e, dict) and str(e.get("session_id") or "") == \
                str(ui_session_id):
            try:
                return int(e.get("pid")), str(e.get("surface") or "")
            except (TypeError, ValueError):
                return None
    return None


#: modules whose presence identifies a process that consumes completion
# events for webui sessions: the Hermes WebUI server drain thread
# (api/background_process, server-side wakeup turns) and the tui/desktop
# per-session notification poller (tui_gateway.server).
BACKEND_MARKERS = ("api.background_process", "tui_gateway.server")

# ---------------------------------------------------------------------------
# process-role gate (v0.4.2, incident pb-20261019T082242Z-8d9299)
# ---------------------------------------------------------------------------
# The v0.4.1 gate vetoed every process with "gateway.run" in sys.modules --
# but the REAL WebUI backend lazily imports gateway.run in-process (agent/
# relay_runtime.py via compression) after it has already hosted completions,
# permanently poisoning its poller.  The role is therefore derived from
# PERSISTENT process identity only (argv, environment, the __main__ file);
# loaded modules are consulted only for processes with unknown argv
# identity.  Parity with the plugin's _webui_* twins is pinned by
# tests/test_webui_event_parity.py.
WEBUI_IDENTITY_ENV = ("HERMES_WEBUI_STATE_DIR", "HERMES_WEBUI_AGENT_DIR")


def _norm_path(p) -> str:
    return str(p or "").replace("\\", "/")


def _path_ends(p, suffix) -> bool:
    """True when the normalized path ends with `suffix` on a path-element
    boundary ('x/gateway/run.py' yes, 'x/mygateway/run.py' no)."""
    p = _norm_path(p)
    return p == suffix or p.endswith("/" + suffix)


def _argv_module(argv, name) -> bool:
    """True when argv runs module `name` (`python -m name ...`)."""
    argv = [str(a) for a in (argv or [])]
    for i in range(len(argv) - 1):
        if argv[i] == "-m" and argv[i + 1] == name:
            return True
    return False


def _main_file_default() -> str:
    import sys
    mf = getattr(sys.modules.get("__main__"), "__file__", None) \
        or (sys.argv[0] if sys.argv else "")
    return str(mf or "")


def process_role(argv=None, environ=None, main_file=None) -> str:
    """'gateway' | 'webui' | 'tui_backend' | 'unknown' from PERSISTENT
    process identity only (argv, environment, the __main__ file -- all
    injectable for tests, defaults to the live process).  Gateway signals
    first (veto before its lazy gateway.run import), then webui markers
    (HERMES_WEBUI_* env, hermes-webui/server.py|bootstrap.py), then the
    tui/desktop runner."""
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
    if any(str(environ.get(k) or "").strip() for k in WEBUI_IDENTITY_ENV) \
            or _path_ends(mf, "hermes-webui/server.py") \
            or _path_ends(mf, "hermes-webui/bootstrap.py"):
        return "webui"
    if _argv_module(argv, "tui_gateway") \
            or _argv_module(argv, "tui_gateway.entry") \
            or _path_ends(mf, "tui_gateway/entry.py"):
        return "tui_backend"
    return "unknown"


def host_decision(ui_session_id: str, *, loaded_modules=None,
                  hermes_home: Path | None = None, argv=None, environ=None,
                  main_file=None) -> tuple[bool, str]:
    """(may_inject, reason) for THIS process (plugin-parity semantics):
    1. an argv/env-identified gateway runner NEVER injects (its delegation
       watcher terminally DROPS completions whose parent it cannot
       resolve), regardless of loaded modules or a live session lease;
    2. a positive webui identity (webui role OR api.background_process, a
       hermes-webui-exclusive module) always hosts -- and MUST win over a
       LATE incidental import of gateway.run (the production regression);
    3. the tui/desktop backend role hosts;
    4. unknown role: a loaded gateway.run still vetoes (v0.4.1 behaviour),
       then the legacy BACKEND_MARKERS allow hosting;
    5. finally the durable lease fallback: this process owns the session."""
    import sys
    mods = loaded_modules if loaded_modules is not None else sys.modules
    role = process_role(argv=argv, environ=environ, main_file=main_file)
    if role == "gateway":
        return False, "gateway-runner"
    if role == "webui" or "api.background_process" in mods:
        return True, "webui-positive"
    if role == "tui_backend":
        return True, "tui-backend"
    if "gateway.run" in mods:
        return False, "gateway-run-loaded"
    if any(m in mods for m in BACKEND_MARKERS):
        return True, "backend-marker"
    owner = lease_owner(ui_session_id, hermes_home=hermes_home)
    if owner and owner[0] == os.getpid():
        return True, "lease-self"
    return False, "no-host"


def may_host_session(ui_session_id: str, *, loaded_modules=None,
                     hermes_home: Path | None = None, argv=None,
                     environ=None, main_file=None) -> bool:
    """True when THIS process can deliver the event natively (the bool view
    of host_decision; see there for the full role-gate semantics).  A
    gateway/CLI process hosting neither never injects -- the request simply
    waits in the spool for the process that does host the session."""
    return host_decision(ui_session_id, loaded_modules=loaded_modules,
                         hermes_home=hermes_home, argv=argv,
                         environ=environ, main_file=main_file)[0]


# ---------------------------------------------------------------------------
# job.json delivery bookkeeping (never changes job status)
# ---------------------------------------------------------------------------

def record_queued(job_id: str, channel: str) -> None:
    with state.job_lock(job_id):
        job = state.load_job(job_id)
        job["delivery"] = {
            "attempted": True, "ok": False, "channel": channel,
            "kind": "webui-event", "reason": "queued", "error": None,
            "turn": len(job.get("turns") or []), "at": state.now_iso(),
        }
        state.save_job(job)


def record_ack(job_id: str, *, ok: bool, reason: str,
               error: str | None = None) -> None:
    try:
        with state.job_lock(job_id):
            job = state.load_job(job_id)
            job["delivery"] = {
                "attempted": True, "ok": ok,
                "channel": job.get("delivery", {}).get("channel"),
                "kind": "webui-event", "reason": reason,
                "error": (error or "")[:300] or None,
                "turn": len(job.get("turns") or []),
                "at": state.now_iso(),
            }
            state.save_job(job)
    except Exception:
        pass
