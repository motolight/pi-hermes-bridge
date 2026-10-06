"""Optional, strictly read-only observation of a job through PI WEB.

PI WEB is NOT part of the bridge's execution model: it never owns a job,
never schedules anything and is never a required dependency.  This module
only answers "would a human see this job's Pi session in the PI WEB UI, and
at which URL?".

Contract of every public function here:
- never raises (all errors become {available: False, reason: ...});
- never writes to job state, never mutates PI WEB beyond the one documented
  POST /api/projects needed to register an existing project;
- every HTTP call has a short timeout and a total time budget;
- proxies are always disabled (this server runs with proxy env vars set,
  and PI WEB is a LAN-local service).

Discovered endpoints (PI WEB 1.202608.2, read from the running service):
  GET  /api/pi-web/status                       health/version
  GET  /api/projects                            [{id,name,path,createdAt}]
  POST /api/projects {"path": p}                register an existing project
  GET  /api/projects/{id}/workspaces            {workspaces:[{id,path,isMain}]}
  GET  /api/machines/local/sessions?cwd=<abs>   sessions read from the standard
       Pi session store, re-read from disk on every request (no cache)
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import state

DEFAULT_BASE = "http://127.0.0.1:8504"
CONFIG_PATH = "~/.config/pi-web/config.json"
# Fallback used only to decide whether registering a cwd makes sense at all,
# i.e. when PI WEB's own config cannot be read.  Registering a path PI WEB
# would refuse is pointless, so we stay conservative.
FALLBACK_ALLOWED_ROOTS = (str(Path.home()),)
DEFAULT_TIMEOUT = 2.0
DEFAULT_BUDGET = 4.0
MAX_BUDGET = 5.0            # observability must never outlive a tool call
MAX_BODY = 256 * 1024
NAME_KEEP = 120             # clamp strings that come from PI WEB payloads
ID_KEEP = 128               # ditto for ids, and therefore for deep links
URL_KEEP = 700
# A POST /api/projects that PI WEB refuses -- or accepts without ever listing
# the project -- is remembered so a broken PI WEB is not re-POSTed on every
# status poll.  The `pi-bridge` CLI is a fresh process per poll, so this lives
# in a small cache file under the bridge home (never job state, no secrets).
REFUSED_REGISTRATIONS_TTL = 300.0
REFUSED_REGISTRATIONS_FILE = "piweb-registration-cache.json"


class _Budget:
    """Wall-clock budget shared by all HTTP calls of one observation."""

    def __init__(self, seconds: float = DEFAULT_BUDGET):
        self.deadline = time.monotonic() + min(MAX_BUDGET,
                                               max(0.2, float(seconds)))

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def timeout(self) -> float | None:
        left = self.left()
        if left <= 0.05:
            return None
        return min(DEFAULT_TIMEOUT, left)

    def expired(self) -> bool:
        return self.left() <= 0.05


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def _cfg_path() -> Path:
    return Path(os.environ.get("PI_WEB_CONFIG", CONFIG_PATH)).expanduser()


def _load_cfg() -> dict:
    try:
        with open(_cfg_path(), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def base_url() -> str:
    """Base URL of the PI WEB web service (env override wins)."""
    env = (os.environ.get("PI_WEB_URL") or "").strip()
    if env:
        base = env if "://" in env else f"http://{env}"
        return base.rstrip("/")
    cfg = _load_cfg()
    host, port = cfg.get("host"), cfg.get("port")
    if host and port:
        return f"http://{host}:{port}".rstrip("/")
    return DEFAULT_BASE


def allowed_roots() -> tuple[str, ...]:
    """Realpaths of the path prefixes PI WEB is allowed to serve."""
    cfg = _load_cfg()
    raw = (cfg.get("pathAccess") or {}).get("allowedPaths")
    roots = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                roots.append(_real(item))
    if not roots:
        roots = [ _real(r) for r in FALLBACK_ALLOWED_ROOTS ]
    return tuple(r for r in roots if r)


def path_allowed(cwd: str) -> bool:
    """True when `cwd` sits inside one of PI WEB's allowed path prefixes."""
    target = _real(cwd)
    if not target:
        return False
    for root in allowed_roots():
        if target == root or target.startswith(root.rstrip("/") + "/"):
            return True
    return False


def _real(path: str) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except Exception:
        return ""


# --------------------------------------------------------------------------
# HTTP (read-only, no proxy, short timeout, never raises)
# --------------------------------------------------------------------------

def _opener():
    # ProxyHandler({}) -> ignore http_proxy/https_proxy/NO_PROXY entirely.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(method: str, path: str, params: dict | None = None,
             body: dict | None = None, budget: _Budget | None = None):
    """Return (status, parsed_json, reason). status None = transport error.

    The budget bounds connect *and* body read: a server that trickles bytes
    or never closes its body cannot outlive the observation window.
    """
    if budget is None:
        timeout, deadline = DEFAULT_TIMEOUT, None
    else:
        timeout, deadline = budget.timeout(), budget.deadline
        if timeout is None:
            return None, None, "time-budget-exhausted"
    timeout = max(0.05, min(float(timeout), DEFAULT_TIMEOUT))
    url = base_url() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener().open(req, timeout=timeout) as resp:
            status = resp.getcode()
            raw = _read_capped(resp, deadline)
            if raw is None:
                return None, None, "read-timeout"
    except urllib.error.HTTPError as e:
        # An HTTP error keeps its code: callers must be able to tell "PI WEB
        # answered 403" from "PI WEB is unreachable" (transient).
        try:
            raw = _read_capped(e, deadline)
        except Exception:
            raw = None
        if not raw:
            return e.code, None, f"http-{e.code}"
        try:
            return e.code, json.loads(raw.decode("utf-8", "replace")), None
        except Exception:
            return e.code, None, f"http-{e.code}"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return None, None, f"unreachable: {type(reason).__name__}: {reason}"
    except (TimeoutError, OSError) as e:
        return None, None, f"unreachable: {type(e).__name__}: {e}"
    except Exception as e:  # defensive: this module must never raise
        return None, None, f"{type(e).__name__}: {e}"
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return status, None, "malformed-json"
    return status, parsed, None


def _read_capped(fp, deadline: float | None) -> bytes | None:
    """Read at most MAX_BODY bytes, giving up when the deadline passes."""
    buf = bytearray()
    while len(buf) <= MAX_BODY:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        try:
            chunk = fp.read(min(64 * 1024, MAX_BODY + 1 - len(buf)))
        except (TimeoutError, OSError):
            return None
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------

def availability(budget: _Budget | None = None) -> dict:
    """GET /api/pi-web/status. {"available": bool, ...} -- never raises."""
    status, parsed, reason = _request("GET", "/api/pi-web/status",
                                      budget=budget)
    if parsed is None:
        return {"available": False, "reason": reason or "no-response",
                "base": base_url()}
    if status is None or status >= 400 or not isinstance(parsed, dict):
        return {"available": False,
                "reason": reason or f"unexpected-status-{status}",
                "base": base_url()}
    return {"available": True, "base": base_url(), "raw": parsed}


def list_projects(budget: _Budget | None = None) -> list[dict]:
    status, parsed, _reason = _request("GET", "/api/projects", budget=budget)
    rows = parsed
    if isinstance(parsed, dict):
        rows = parsed.get("projects")
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def find_project(cwd: str, budget: _Budget | None = None) -> dict | None:
    target = _real(cwd)
    if not target:
        return None
    for proj in list_projects(budget=budget):
        if _real(str(proj.get("path") or "")) == target:
            return proj
    return None


def _refused_cache_file() -> Path:
    return state.bridge_home() / REFUSED_REGISTRATIONS_FILE


def _load_refused() -> dict:
    """Un-expired {abs path: deadline epoch}; empty on any problem."""
    try:
        with open(_refused_cache_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        now = time.time()
        return {k: v for k, v in data.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
                and v > now}
    except Exception:
        return {}


def _mark_registration_refused(path: str, cache: dict) -> None:
    """Best effort: a cache that cannot be written must not break anything."""
    cache[path] = time.time() + REFUSED_REGISTRATIONS_TTL
    try:
        state.write_text_atomic(_refused_cache_file(), json.dumps(cache))
    except Exception:
        pass


def register_project(cwd: str, budget: _Budget | None = None) -> dict | None:
    """One POST /api/projects for an existing directory, then confirm by GET.

    Returns the project dict or None.  This is PI WEB's normal mechanism for
    adding a project; it does not change any trust setting.  Only a real
    refusal (HTTP 4xx/5xx) or a registration PI WEB accepted but never lists
    is memoized; transport problems stay transient so the next poll retries.
    """
    target = _real(cwd)
    if not target:
        return None
    cache = _load_refused()
    if target in cache:
        return None
    status, _parsed, _reason = _request("POST", "/api/projects",
                                        body={"path": target}, budget=budget)
    if status is None:
        return None
    if status >= 400:
        _mark_registration_refused(target, cache)
        return None
    proj = find_project(target, budget=budget)
    if proj is None:
        _mark_registration_refused(target, cache)
    return proj


def ensure_project(cwd: str, budget: _Budget | None = None) -> dict | None:
    """Find the PI WEB project for `cwd`, registering it once if missing.

    Paths outside PI WEB's allowed roots are never registered (the request
    would be refused anyway): the caller reports that as no observability.
    """
    if not path_allowed(cwd):
        return None
    if budget is not None and budget.expired():
        return None
    proj = find_project(cwd, budget=budget)
    if proj is not None:
        return proj
    if budget is not None and budget.expired():
        return None
    return register_project(cwd, budget=budget)


def workspace_for(project, budget: _Budget | None = None) -> dict | None:
    pid = str((project or {}).get("id") or "")
    if not pid:
        return None
    if budget is not None and budget.expired():
        return None
    status, parsed, _reason = _request(
        "GET", f"/api/projects/{urllib.parse.quote(pid, safe='')}/workspaces",
        budget=budget)
    rows = parsed
    if isinstance(parsed, dict):
        rows = parsed.get("workspaces")
    if status is None or not isinstance(rows, list):
        return None
    workspaces = [w for w in rows if isinstance(w, dict)]
    if not workspaces:
        return None
    target = _real(str((project or {}).get("path") or ""))
    for w in workspaces:
        if w.get("isMain"):
            return w
    for w in workspaces:
        if _real(str(w.get("path") or "")) == target:
            return w
    return workspaces[0]


def session_visible(cwd: str, session_id: str,
                    budget: _Budget | None = None) -> dict:
    """Is this session id listed by PI WEB for `cwd`? Read-only, no cache."""
    if not session_id:
        return {"visible": False, "reason": "no-session-id"}
    status, parsed, reason = _request(
        "GET", "/api/machines/local/sessions",
        params={"cwd": _real(cwd) or cwd}, budget=budget)
    if status is None:
        return {"visible": False, "reason": reason or "unreachable"}
    rows = parsed
    if isinstance(parsed, dict):
        rows = parsed.get("sessions")
    if not isinstance(rows, list):
        return {"visible": False, "reason": "unexpected-sessions-payload"}
    for s in rows:
        if isinstance(s, dict) and str(s.get("id") or "") == session_id:
            count = s.get("messageCount")
            modified = s.get("modified")
            return {
                "visible": True,
                "message_count": count if isinstance(count, int) else None,
                "modified": str(modified)[:NAME_KEEP] if modified else None,
            }
    return {"visible": False, "reason": "session-not-listed"}


def deep_link(project_id: str | None, workspace_id: str | None,
              session_id: str | None) -> str | None:
    if not (project_id and workspace_id and session_id):
        return None
    q = urllib.parse.urlencode({"machine": "local",
                                "project": str(project_id)[:ID_KEEP],
                                "workspace": str(workspace_id)[:ID_KEEP],
                                "session": str(session_id)[:ID_KEEP]})
    return f"{base_url()}/?{q}"[:URL_KEEP]


# --------------------------------------------------------------------------
# one-shot observation for a job (the only thing bridge.py calls)
# --------------------------------------------------------------------------

def _unavailable(reason: str, extra: dict | None = None) -> dict:
    out = {"available": False, "reason": reason}
    out.update(extra or {})
    return out


def observe(cwd: str, session_id: str,
            budget_seconds: float = DEFAULT_BUDGET) -> dict:
    """Read-only PI WEB view of one job. Never raises, always returns a dict.

    Shape on success:
      {available, project_id, project_name, workspace_id, pi_web_url,
       session_visible, message_count}
    On any problem: {available: false, reason: "..."} (+ partial fields).
    """
    budget = _Budget(budget_seconds)
    if not path_allowed(cwd):
        return _unavailable("cwd-outside-allowed-paths",
                            {"cwd": _real(cwd) or str(cwd)})

    avail = availability(budget=budget)
    if not avail["available"]:
        return _unavailable(avail.get("reason") or "unreachable",
                            {"pi_web_url_base": avail.get("base")})
    base = avail["base"]

    partial = {"pi_web_url_base": base}
    if budget.expired():
        return _unavailable("time-budget-exhausted", partial)

    proj = ensure_project(cwd, budget=budget)
    if proj is None:
        if budget.expired():
            return _unavailable("time-budget-exhausted", partial)
        return _unavailable("project-not-registered", partial)

    partial.update({
        # everything that comes back from PI WEB is clamped: this dict is
        # forwarded to the model by the Hermes plugin.
        "project_id": str(proj.get("id") or "")[:ID_KEEP] or None,
        "project_name": (str(proj["name"])[:NAME_KEEP]
                         if proj.get("name") is not None else None),
    })

    ws = workspace_for(proj, budget=budget)
    workspace_id = (str(ws.get("id") or "")[:ID_KEEP] if ws else None)

    vis = session_visible(cwd, session_id, budget=budget)
    link = deep_link(partial.get("project_id"), workspace_id, session_id)

    out = {
        "available": True,
        "project_id": partial.get("project_id"),
        "project_name": partial.get("project_name"),
        "workspace_id": workspace_id,
        "pi_web_url": link,
        "session_visible": bool(vis.get("visible")),
        "message_count": vis.get("message_count"),
    }
    if not out["session_visible"] and vis.get("reason"):
        out["session_reason"] = vis["reason"]
    if out["session_visible"] and vis.get("modified"):
        out["session_modified"] = vis["modified"]
    return out


def observe_job(job: dict, budget_seconds: float = DEFAULT_BUDGET) -> dict:
    try:
        return observe(str(job.get("cwd") or ""),
                       str(job.get("pi_session_id") or ""),
                       budget_seconds=budget_seconds)
    except Exception as e:  # belt and braces: observability never breaks a job
        return _unavailable(f"internal: {type(e).__name__}: {e}")
