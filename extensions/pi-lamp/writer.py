#!/usr/bin/env python3
"""pi-lamp writer: snapshot of pi-bridge jobs -> WebUI-readable status.json.

Purely LOCAL reads: job.json / turn logs / runner.log under PI_BRIDGE_HOME,
cheap liveness probes (os.kill, systemctl is-active), and GET-only HTTP to the
local PI WEB observability service for the deep link (no POST, no PI WEB state
mutation, private-address guard, one per-tick time budget).  It imports no
bridge code, writes nothing inside PI_BRIDGE_HOME, never calls any LLM, and
never emits task text, cwd paths or secrets into the status file (paths in
`error` are redacted).

Per job the reader gets a `group` ("active" | "recent" | "quiet" | "aged")
plus `finished_at` / `finished_age_s`, computed with the WRITER's clock so the
browser's clock skew cannot change when a finished job stops taking card
space.  Groups are additive: an older reader keeps using `view`, and an older
status.json (no group/finished_at) still renders client-side from `view` +
`updated_at`.

Output (atomic): PI_LAMP_OUT (default
~/.hermes/webui/extensions/pi-lamp/status.json), fetched by the pi-lamp
extension JS same-origin at /extensions/pi-lamp/status.json.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.1.0"

JOB_ID_RE = re.compile(r"^pb-\d{8}T\d{6}Z-[0-9a-f]{6}$")
UNIT_RE = re.compile(r"^pi-bridge-pb-\d{8}T\d{6}Z-[0-9a-f]{6}-t\d{1,4}$")
ACTIVE_STATUSES = {"queued", "running"}
ACTIVE_VIEWS = ("running", "stalled")
# Finished-quietly views: they leave the card a while after finishing (they
# are still counted in the static badge) because "it worked" is rarely news
# three hours later; failures stay visible until acknowledged.
QUIET_VIEWS = frozenset({"done", "cancelled"})
TERMINAL_VIEWS = {
    "completed": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "interrupted",
}


def env_s(name: str, default: str) -> str:
    v = (os.environ.get(name) or "").strip()
    return v or default


def env_f(name: str, default: float) -> float:
    try:
        return float(env_s(name, str(default)))
    except ValueError:
        return default


BRIDGE_HOME = Path(env_s("PI_BRIDGE_HOME", str(Path.home() / ".local" / "state" / "pi-bridge"))).expanduser()
OUT_PATH = Path(env_s("PI_LAMP_OUT",
                      str(Path.home() / ".hermes/webui/extensions/pi-lamp/status.json"))).expanduser()
STALL_S = env_f("PI_LAMP_STALL_S", 420.0)          # "no activity" threshold for the alarm view
STARTUP_GRACE_S = env_f("PI_LAMP_STARTUP_GRACE_S", 15.0)
RESULT_TTL_S = env_f("PI_LAMP_RESULT_TTL_S", 7 * 86400.0)  # terminal jobs stay on the lamp this long
DONE_QUIET_S = env_f("PI_LAMP_DONE_QUIET_S", 3 * 3600.0)   # done leaves the CARD this long after finishing
NOTABLE_WINDOW_S = env_f("PI_LAMP_NOTABLE_WINDOW_S", 24 * 3600.0)  # older terminal jobs are counted nowhere
MAX_JOBS = int(env_f("PI_LAMP_MAX_JOBS", 200))
PIWEB_ENABLED = env_s("PI_LAMP_PIWEB", "1") != "0"
PIWEB_TTL_S = env_f("PI_LAMP_PIWEB_TTL_S", 24 * 3600.0)     # observe terminal jobs updated within this window
PIWEB_REOBSERVE_S = env_f("PI_LAMP_PIWEB_REOBSERVE_S", 300.0)
PIWEB_CONFIG = Path(env_s("PI_WEB_CONFIG",
                          str(Path.home() / ".config" / "pi-web" / "config.json"))).expanduser()
HTTP_BUDGET_S = env_f("PI_LAMP_HTTP_BUDGET_S", 4.0)   # ALL HTTP work per writer run
HTTP_TIMEOUT_S = env_f("PI_LAMP_HTTP_TIMEOUT_S", 1.2)
META_TTL_S = env_f("PI_LAMP_PIWEB_META_TTL_S", 300.0)  # projects/workspaces map re-read this often
NEG_TTL_S = env_f("PI_LAMP_PIWEB_NEG_TTL_S", 120.0)    # "pi-web unreachable" is retried this often
PI_SESSIONS_ROOT = Path(env_s("PI_LAMP_PI_SESSIONS",
                              str(Path.home() / ".pi/agent/sessions"))).expanduser()
CACHE_PATH = Path(env_s("PI_LAMP_CACHE",
                        str(Path.home() / ".local/state/pi-lamp/piweb-cache.json"))).expanduser()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------- liveness

def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False


def _unit_active(unit) -> bool:
    if not unit or not UNIT_RE.match(str(unit)):
        return False
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "--quiet", str(unit)],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def runner_alive(job: dict) -> bool:
    """Mirror pi_bridge.bridge.runner_alive (read-only, no persistence)."""
    lr = job.get("last_run") or {}
    if _pid_alive(lr.get("pid")):
        return True
    if lr.get("launcher") == "systemd" and _unit_active(lr.get("unit")):
        return True
    return False


# ---------------------------------------------------------------- cache helpers

META_PREFIX = "__"  # meta keys (projects map, workspace ids) are never pruned


def load_cache() -> dict:
    """Shape-validated: a hand-edited or half-written cache must never be able
    to wedge a writer tick (an unexpected type here would otherwise raise
    inside job_view, where main() only catches OSError/ValueError)."""
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    clean: dict = {}
    for k, v in d.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        ts = v.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue
        if "map" in v and not isinstance(v["map"], dict):
            continue
        if "ttl" in v and (isinstance(v["ttl"], bool) or not isinstance(v["ttl"], (int, float))):
            continue
        clean[k] = v
    return clean


def save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_name(f".tmp-{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.chmod(tmp, 0o600)  # the map keys are local absolute paths
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


# ------------------------------------------------ pi-web deep links (GET-only, local)
#
# The PI WEB link is assembled from the local PI WEB config file (base URL)
# plus anonymous GETs on that same local service.  Nothing is ever POSTed — PI
# WEB's state is not ours to mutate — 3xx redirects are never followed (a
# redirect target would be an unvalidated host), the configured host must be a
# loopback/private IP literal so no request can leave this box, the work is
# bounded by one per-tick budget, and every answer is cached in PI_LAMP_CACHE
# (never inside PI_BRIDGE_HOME).  A job whose cwd PI WEB does not know simply
# has no link.

URL_KEEP, ID_KEEP, NAME_KEEP = 400, 64, 80
_MAX_BODY = 512 * 1024
_PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "fc00::/7"))


class Budget:
    """Wall-clock bound for every HTTP request made by one writer run."""

    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + max(0.0, float(seconds))

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def timeout(self) -> float:
        return max(0.05, min(HTTP_TIMEOUT_S, self.deadline - time.monotonic()))


_opener = None


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the Location host is not the address we
    validated with is_local_host(), so a 3xx is simply a failed read."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _http_opener():
    global _opener
    if _opener is None:
        # ProxyHandler({}) => http_proxy/https_proxy are ignored entirely: a
        # local observability read must never reach an egress proxy.
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                              _RefuseRedirects())
    return _opener


def _real(path) -> str:
    try:
        return str(Path(str(path)).expanduser().resolve())
    except Exception:
        return ""


def is_local_host(host: str) -> bool:
    """True only for an IP literal in loopback/RFC1918/link-local space. Host
    names are deliberately not resolved: picking the address ourselves is what
    keeps the promise that no request leaves this box."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not ip.is_unspecified and any(ip in net for net in _PRIVATE_NETS)


_base_memo = {"done": False, "base": None}


def piweb_base() -> str | None:
    """Base URL of the LOCAL PI WEB, or None (config missing/unreadable, or the
    configured host is not a private IP).  The writer is a short-lived oneshot
    process, so the config is read once per run and reused for every job."""
    if _base_memo["done"]:
        return _base_memo["base"]
    base = None
    try:
        cfg = json.loads(PIWEB_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        cfg = None
    if isinstance(cfg, dict):
        host, port = cfg.get("host"), cfg.get("port")
        if isinstance(host, str) and host and isinstance(port, int) and not isinstance(port, bool):
            h = host.strip().strip("[]")
            if h.lower() in ("localhost", "localhost.localdomain"):
                h = "127.0.0.1"      # resolve the one name we accept, ourselves
            if is_local_host(h):
                if ":" in h:
                    h = f"[{h}]"     # IPv6 literal needs brackets in a URL
                base = f"http://{h}:{port}"
    _base_memo["done"], _base_memo["base"] = True, base
    return base


def _read_capped(fp, deadline: float) -> bytes | None:
    buf = bytearray()
    while len(buf) <= _MAX_BODY:
        if time.monotonic() >= deadline:
            return None
        try:
            chunk = fp.read(min(64 * 1024, _MAX_BODY + 1 - len(buf)))
        except (TimeoutError, OSError):
            return None
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def http_get(base: str, path: str, budget: Budget):
    """GET only, body-capped, never raises: (status, parsed_json) or (None, None)."""
    if budget.expired():
        return None, None
    req = urllib.request.Request(base + path,
                                 headers={"Accept": "application/json"}, method="GET")
    try:
        with _http_opener().open(req, timeout=budget.timeout()) as resp:
            code, raw = resp.getcode(), _read_capped(resp, budget.deadline)
    except Exception:
        return None, None
    if raw is None:
        return None, None
    try:
        return code, json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return code, None


def _rows(parsed, key: str):
    rows = parsed.get(key) if isinstance(parsed, dict) else parsed
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None


def _cache_get(cache: dict, key: str, ttl: float, now: float, stale: bool = False):
    ent = cache.get(key)
    if not isinstance(ent, dict):
        return None
    if stale:
        return ent
    # an entry may carry its own (usually shorter) ttl, e.g. a negative answer
    own = ent.get("ttl")
    if isinstance(own, (int, float)) and not isinstance(own, bool):
        ttl = float(own)
    if (now - float(ent.get("ts") or 0)) < ttl:
        return ent
    return None


def _stale_map(cache: dict, key: str) -> dict:
    m = (_cache_get(cache, key, 1e12, 0.0, stale=True) or {}).get("map")
    return m if isinstance(m, dict) else {}


def _stale_id(cache: dict, key: str):
    v = (_cache_get(cache, key, 1e12, 0.0, stale=True) or {}).get("id")
    return v if isinstance(v, str) else None


def projects_map(base: str, budget: Budget, cache: dict, now: float) -> dict:
    """{realpath: {id, name}} of PI WEB's registered projects; cached, {} on failure."""
    key = META_PREFIX + "projects__"
    ent = _cache_get(cache, key, META_TTL_S, now)
    if ent is not None:
        m = ent.get("map")
        return m if isinstance(m, dict) else {}
    if budget.expired():
        return _stale_map(cache, key)
    status, parsed = http_get(base, "/api/projects", budget)
    mapping: dict[str, dict] = {}
    rows = _rows(parsed, "projects")
    if status == 200 and rows is not None:
        for r in rows:
            pid, path = str(r.get("id") or ""), _real(r.get("path") or "")
            if pid and path:
                mapping[path] = {"id": pid[:ID_KEEP], "name": str(r.get("name") or "")[:NAME_KEEP]}
        ttl = META_TTL_S
    else:
        ttl = NEG_TTL_S  # unreachable: retry soon, keep serving the last good map
    cache[key] = {"ts": now, "ttl": ttl,
                  "map": mapping or _stale_map(cache, key)}
    _cache_prune(cache)
    return cache[key]["map"]


def workspace_id(base: str, project_id: str, project_path: str,
                 budget: Budget, cache: dict, now: float) -> str | None:
    key = f"{META_PREFIX}ws__{project_id}"
    ent = _cache_get(cache, key, META_TTL_S, now)
    if ent is not None:
        v = ent.get("id")
        return v if isinstance(v, str) else None
    if budget.expired():
        return _stale_id(cache, key)
    status, parsed = http_get(base, f"/api/projects/{urllib.parse.quote(project_id, safe='')}/workspaces", budget)
    rows = _rows(parsed, "workspaces") if status == 200 else None
    chosen = None
    if rows:
        chosen = next((w for w in rows if w.get("isMain")), None) \
            or next((w for w in rows if _real(w.get("path") or "") == project_path), None) \
            or rows[0]
    if chosen is None:
        # A transient failure must not erase a link that worked a minute ago:
        # keep the previous answer but retry on the shorter negative ttl.
        cache[key] = {"ts": now, "ttl": NEG_TTL_S, "id": _stale_id(cache, key)}
    else:
        cache[key] = {"ts": now, "id": str(chosen.get("id") or "")[:ID_KEEP]}
    _cache_prune(cache)
    return cache[key]["id"]


def _deep_link(base: str, project_id, workspace, session_id) -> str | None:
    if not (project_id and workspace and session_id):
        return None
    q = urllib.parse.urlencode({"machine": "local", "project": str(project_id)[:ID_KEEP],
                                "workspace": str(workspace)[:ID_KEEP],
                                "session": str(session_id)[:ID_KEEP]})
    return f"{base}/?{q}"[:URL_KEEP]


def _cache_prune(cache: dict) -> None:
    if len(cache) <= 400:
        return
    meta = {k: v for k, v in cache.items() if k.startswith(META_PREFIX)}
    jobs = sorted(((k, v) for k, v in cache.items() if not k.startswith(META_PREFIX)),
                  key=lambda kv: float((kv[1] or {}).get("ts") or 0))
    cache.clear()
    cache.update(meta)
    cache.update(jobs[-200:])


def piweb_view(job: dict, cache: dict, now: float, budget: Budget) -> dict | None:
    """PI WEB deep link for one job, from read-only local sources.  Cached;
    only computed for interesting jobs (active or recently terminal)."""
    sid = job.get("pi_session_id")
    cwd = job.get("cwd")
    if not PIWEB_ENABLED or not (isinstance(sid, str) and sid) or not (isinstance(cwd, str) and cwd):
        return None
    updated = parse_iso(job.get("updated_at")) or 0.0
    active = job.get("status") in ACTIVE_STATUSES
    if not active and (now - updated) > PIWEB_TTL_S:
        return None
    key = f'{job.get("job_id")}|{job.get("status")}|{job.get("updated_at")}'
    ent = _cache_get(cache, key, PIWEB_REOBSERVE_S if active else 3600.0, now)
    if ent is not None:
        return ent.get("view")
    stale = _cache_get(cache, key, 1e12, now, stale=True)
    base = piweb_base()
    if base is None or budget.expired():
        # No config, or this tick spent its HTTP budget: keep the last known
        # link instead of poisoning the cache with a fresh negative answer.
        return stale["view"] if stale else None
    path = _real(cwd)
    proj = projects_map(base, budget, cache, now).get(path)
    view = None
    if proj:
        link = _deep_link(base, proj["id"],
                          workspace_id(base, proj["id"], path, budget, cache, now), sid)
        if link:
            view = {"available": True, "url": link}
            if proj.get("name"):
                view["project_name"] = proj["name"]
    cache[key] = {"ts": now, "view": view}
    _cache_prune(cache)
    return view


# ---------------------------------------------------------------- job view

def session_keys(job: dict) -> list[str]:
    origin = job.get("origin") or {}
    if origin.get("platform") != "webui":
        return []
    keys = []
    for k in ("ui_session_id", "chat_id"):
        v = origin.get(k)
        if isinstance(v, str) and v and v not in keys:
            keys.append(v)
    return keys


def pi_session_mtime(job: dict) -> float:
    """Live activity signal: pi appends its session JSONL incrementally
    (~/.pi/agent/sessions/<cwd>/<start>_<session_id>.jsonl), so its mtime
    advances with every assistant message / tool call, while turn stdout is
    only flushed at turn end.  Returns 0 when the file is not found."""
    sid = job.get("pi_session_id")
    if not isinstance(sid, str) or not re.match(r"^[0-9a-fA-F-]{36}$", sid):
        return 0.0
    try:
        # job.json carries the authoritative path; trust it only when it really
        # is this session's file inside the pi sessions tree.
        pf = job.get("pi_session_file")
        if isinstance(pf, str) and pf and sid in Path(pf).name:
            p = Path(pf)
            try:
                if p.is_file() and PI_SESSIONS_ROOT in p.resolve().parents:
                    return mtime(p)
            except OSError:
                pass
        best = 0.0
        for pattern in (f"*/*_{sid}.jsonl", f"*/*{sid}*.jsonl"):
            for p in PI_SESSIONS_ROOT.glob(pattern):
                m = mtime(p)
                if m > best:
                    best = m
            if best:
                break
        return best
    except OSError:
        return 0.0


def activity_ts(job_dir: Path, job: dict, now: float) -> float:
    """Best local activity signal: newest of the live pi session JSONL,
    job.json / runner.log / turn stdout+stderr mtimes, floored by updated_at."""
    best = max(mtime(job_dir / "job.json"), mtime(job_dir / "runner.log"),
               pi_session_mtime(job))
    tdir = job_dir / "turns"
    try:
        for p in tdir.iterdir():
            if p.is_file() and p.suffix in (".out", ".err", ".log"):
                m = mtime(p)
                if m > best:
                    best = m
    except OSError:
        pass
    upd = parse_iso(job.get("updated_at")) or 0.0
    return max(best, upd)


def finished_ts(job: dict, turns: list[dict]) -> float | None:
    """When the work actually stopped: the newest turn `finished_at`, falling
    back to `updated_at` (a runner that was killed records no finished_at).
    None when the job carries no usable timestamp at all."""
    best = 0.0
    for t in turns:
        f = parse_iso(t.get("finished_at"))
        if f and f > best:
            best = f
    if best <= 0:
        best = parse_iso(job.get("updated_at")) or 0.0
    return best or None


def group_of(view: str, finished_age_s: float | None) -> str:
    """UI group, decided once by the writer's clock:
      active – running/stalled, always listed;
      recent – terminal and still worth a row in the card;
      quiet  – finished (done/cancelled) a while ago: counted in the badge,
               no longer takes card space;
      aged   – terminal and older than the day: neither counted nor listed.
    Failures are never `quiet`: acknowledging is the only way to hide them."""
    if view in ACTIVE_VIEWS:
        return "active"
    if finished_age_s is None:
        return "recent"           # unknown age: show it rather than hide it
    if finished_age_s > NOTABLE_WINDOW_S:
        return "aged"
    if view in QUIET_VIEWS and finished_age_s > DONE_QUIET_S:
        return "quiet"
    return "recent"


def duration_s(job: dict, now: float, view: str) -> float:
    """Elapsed time, counted up only while the job is active *as a view*.  A
    terminal job — including one the bridge still calls `running` while its
    runner vanished (view `interrupted`) — stops at its last recorded end
    instead of ticking forever in the card."""
    start = parse_iso(job.get("created_at"))
    if start is None:
        return 0.0
    if view in ACTIVE_VIEWS:
        return max(0.0, now - start)
    turns = [t for t in (job.get("turns") or []) if isinstance(t, dict)]
    end = finished_ts(job, turns) or 0.0
    if end <= start:  # never recorded an end: freeze at the last update
        end = max(parse_iso(job.get("updated_at")) or 0.0, start)
    return max(0.0, end - start)


_PATH_RE = re.compile(r"/[A-Za-z0-9._~+-]+(?:/[A-Za-z0-9._~+-]+)*")
_HOME_RE = re.compile(r"~/[A-Za-z0-9._~+/-]+")


def redact_error(err) -> str | None:
    """Short, path-free error line for the card: status.json is served over
    HTTP and must not carry the local filesystem layout."""
    if not err:
        return None
    s = str(err)[:400].replace(str(Path.home()), "~")
    s = _HOME_RE.sub("<path>", s)
    s = _PATH_RE.sub("<path>", s)
    s = s[:200]
    return re.sub(r"<p(?:a(?:t(?:h)?)?)?$", "", s)  # never end on a cut placeholder


def job_view(job_dir: Path, job: dict, now: float, cache: dict, budget: Budget) -> dict | None:
    keys = session_keys(job)
    if not keys:
        return None
    status = job.get("status") or ""
    turns = [t for t in (job.get("turns") or []) if isinstance(t, dict)]
    last_turn = turns[-1] if turns else None

    act = 0.0
    if status in ACTIVE_STATUSES:
        alive = runner_alive(job)
        act = activity_ts(job_dir, job, now)
        age = max(0.0, now - act)
        if alive:
            view = "stalled" if age > STALL_S else "running"
        else:
            started = (parse_iso((job.get("last_run") or {}).get("started_at"))
                       or parse_iso(job.get("updated_at")) or now)
            if (now - started) < STARTUP_GRACE_S:
                view = "running"  # systemd unit may still be spawning the runner
            else:
                view = "interrupted"  # runner vanished; bridge reconciles lazily
    else:
        view = TERMINAL_VIEWS.get(status, "interrupted")
        upd = parse_iso(job.get("updated_at")) or 0.0
        if (now - upd) > RESULT_TTL_S:
            return None

    delivery = job.get("delivery") or {}
    wake = job.get("wake") or {}
    out = {
        "job_id": job.get("job_id"),
        "view": view,
        "status": status,
        "sessions": keys,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "duration_s": round(duration_s(job, now, view), 1),
        "finished_at": None,
        "finished_age_s": None,
        "group": "active",
        "turn_n": (last_turn or {}).get("n"),
        "turn_kind": (last_turn or {}).get("kind"),
        "turn_started_at": (last_turn or {}).get("started_at"),
        "turns_total": len(turns),
        "exit_code": (last_turn or {}).get("exit_code") if view in ("done", "failed", "cancelled", "interrupted") else None,
        "error": redact_error(job.get("error")),
        "delivery": {
            "attempted": bool(delivery.get("attempted")),
            "ok": bool(delivery.get("ok")),
            "channel": delivery.get("channel"),
            "reason": delivery.get("reason"),
        } if delivery else None,
        "wake_delivered": bool(wake.get("delivered")),
        "result_chars": int(job.get("final_result_chars") or 0),
    }
    if view in ACTIVE_VIEWS:
        out["activity_age_s"] = round(max(0.0, now - act), 1)
    else:
        # When it ended, and how long ago — the card's auto-hide and the
        # "in the last day" window are both computed from this, on the
        # writer's clock (a skewed browser clock must not move the goalposts).
        fts = finished_ts(job, turns)
        out["finished_at"] = iso(fts) if fts else None
        out["finished_age_s"] = round(max(0.0, now - fts), 1) if fts else None
        out["group"] = group_of(view, out["finished_age_s"])
    try:
        pwv = piweb_view(job, cache, now, budget)
    except Exception:
        pwv = None  # a link is decoration; it must never cost a snapshot tick
    if pwv:
        out["pi_web"] = pwv
    return out


# ---------------------------------------------------------------- main

def main() -> int:
    now = time.time()
    budget = Budget(HTTP_BUDGET_S)
    jobs_root = BRIDGE_HOME / "jobs"
    cache = load_cache()
    views = []
    if jobs_root.is_dir():
        for d in sorted(jobs_root.iterdir(), reverse=True):
            if not (d.is_dir() and JOB_ID_RE.match(d.name)):
                continue
            try:
                with open(d / "job.json", "r", encoding="utf-8") as f:
                    job = json.load(f)
                if job.get("job_id") != d.name:
                    continue
                v = job_view(d, job, now, cache, budget)
                if v:
                    views.append(v)
            except (OSError, ValueError):
                continue
            if len(views) >= MAX_JOBS * 2:
                break
    order = {"stalled": 0, "running": 1, "interrupted": 2, "failed": 3, "cancelled": 4, "done": 5}
    gorder = {"active": 0, "recent": 1, "quiet": 2, "aged": 3}
    # active jobs first, then the still-interesting terminal ones; the MAX_JOBS
    # cut therefore drops the oldest/least interesting tail, not the work.
    views.sort(key=lambda v: (gorder.get(v["group"], 9), order.get(v["view"], 9),
                              -(parse_iso(v.get("updated_at")) or 0)))
    views = views[:MAX_JOBS]
    payload = {"generated_at": now_iso(), "writer_version": VERSION,
               "stall_threshold_s": STALL_S,
               # the reader's fallback for a snapshot without per-job groups,
               # so client and writer agree on the same window either way
               "done_quiet_s": DONE_QUIET_S, "notable_window_s": NOTABLE_WINDOW_S,
               "jobs": views}

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_name(f".tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)  # the route is auth-gated, but local users need not read job state
    os.replace(tmp, OUT_PATH)
    save_cache(cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
