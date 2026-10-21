"""Stall watchdog: detect, diagnose and safely recover stalled Pi jobs.

Why this exists (the 2026-10-21 incident, pb-20261021T191120Z-532dc0)
---------------------------------------------------------------------
A Pi job ran a foreground descendant command (``bash -c ssh ... powershell
-File capture.ps1``) whose script looped forever.  The runner stayed alive,
Pi stayed alive, but the session JSONL went stale for 33-52 minutes: the
job hung silently waiting for a tool call that would never return, and
nothing noticed until a human looked.  The manual recovery -- SIGTERM to
the *verified descendant subtree only*, keeping Pi itself alive -- restored
the session in seconds and Pi continued reasoning in the same session.
See docs/STALL_WATCHDOG.md.

Design
------
* SERVER-SIDE and independent of any browser / WebUI / pi-lamp connection:
  a systemd user timer runs `pi-bridge watchdog tick` once a minute.
* STALL DETECTION is never a single timer.  "No JSONL for N minutes" only
  *suspects*; a RECOVERED verdict requires multiple independent signals
  (see `diagnose`): job running, runner alive, Pi process alive and
  identity-verified, activity stale (session JSONL / turn stdout / runner
  log), a long-lived verified *descendant tool command* of Pi with zero
  CPU across repeated /proc samples, and (when a backend metrics URL is
  configured) no active LLM generation on that backend.  A long build or
  test with CPU activity or periodic output can never satisfy this.
* TIER 1 auto-recovery interrupts ONLY verified descendant tool commands
  of the specific bridge job: every PID's ancestry is re-verified against
  the job's recorded Pi PID (start-time identity, PID-reuse safe), the Pi
  and runner PIDs themselves are excluded, SIGTERM first with a bounded
  grace, then SIGKILL only of the surviving descendants of that subtree.
  Pi keeps running, so the hung tool call returns an error to Pi and Pi
  continues in the SAME session.  Tier 2 (the Pi/model call itself hung)
  is only ever *diagnosed and reported* -- never auto-killed.
* NEVER touches job.status.  The watchdog state lives in the additive
  ``job["watchdog"]`` block; terminal jobs belong to the normal v0.4.2
  completion pipeline, and a tick that races job completion simply finds
  the job non-active and does nothing.
* Every mutation takes state.job_lock and re-reads job.json under it.
  Ticks are serialised by a non-blocking flock, so two ticks can never
  run recovery concurrently.  All signals re-verify PID identity at the
  moment of signalling.
* Hermes integration mirrors the completion-wake reliability contract with
  a SEPARATE event type and policy: durable spooled ``pi_bridge_stall``
  events are POSTed to the bridge-owned `pi-bridge-stall` webhook route
  (same HMAC V2 scheme and port as the completion route), retried on
  every later tick until accepted or expired.  `needs_attention` also
  sends a direct user message along the job's origin (messaging
  platforms only, same argv-only `hermes send` path as origin delivery;
  the webui channel learns it from the wake run and the pi-lamp).

Configuration (all optional; absent == enabled with these defaults)
-------------------------------------------------------------------
``<bridge_home>/config.json``::    {"watchdog": {...}}
env overrides ``PI_BRIDGE_WATCHDOG_<UPPER_KEY>``:

    enabled                  true
    stall_after_s            900     silence before suspicion (10-15 min)
    startup_grace_s          90      a fresh turn is not a stall
    cpu_sample_interval_s    2.0     between /proc CPU samples
    term_grace_s             5.0     SIGTERM -> SIGKILL grace (tier 1)
    confirm_grace_s          180     wait for JSONL to resume after tier 1
    max_recovery_attempts    2       then needs_attention (never more)
    backend_metrics_url      ""      e.g. http://host:8000/metrics (vLLM);
                                     "" disables that signal
    event_expire_s           2700    stall-event retry budget (~45 min,
                                     same window as the completion wake)
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import signal
import subprocess
import time
import urllib.request
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import deliver, runner, state, wake
from .state import BridgeError

VERSION = "0.5.0"

STALL_EVENT = "pi_bridge_stall"
STALL_ROUTE_NAME = "pi-bridge-stall"

WATCHDOG_STATES = ("healthy", "suspected_stall", "diagnosing", "recovering",
                   "recovered", "needs_attention")

DEFAULTS = {
    "enabled": True,
    "stall_after_s": 900.0,
    "startup_grace_s": 90.0,
    "cpu_sample_interval_s": 2.0,
    "term_grace_s": 5.0,
    "confirm_grace_s": 180.0,
    "max_recovery_attempts": 2,
    "backend_metrics_url": "",
    "event_expire_s": 2700.0,
    "descendant_min_age_s": 5.0,
}

CMDLINE_KEEP = 300
DIAG_SUMMARY_KEEP = 500
# A tool command must predate the observation window to be "the command the
# silent session is waiting on".  A command spawned seconds ago would mean
# the session was still producing activity -- and fresh activity already
# stops the pipeline long before diagnosis, so this is a second net against
# racing a just-spawned tool, not the primary guard.
DESCENDANT_MIN_AGE_S = 5.0
# Tier-2 suspicion (Pi silent, NO descendant to interrupt) becomes
# needs_attention only after this many consecutive identical diagnoses --
# one flaky sample must never page a human.
TIER2_REPEAT_BEFORE_ATTENTION = 2


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def _env_overrides() -> dict:
    out = {}
    for key, default in DEFAULTS.items():
        raw = (os.environ.get("PI_BRIDGE_WATCHDOG_" + key.upper()) or "").strip()
        if not raw:
            continue
        if isinstance(default, bool):
            out[key] = raw.lower() not in ("0", "false", "no", "off")
        elif isinstance(default, str):
            out[key] = raw
        else:
            try:
                out[key] = float(raw)
            except ValueError:
                pass  # keep the lower-priority value on a malformed env
    return out


def load_config() -> dict:
    """defaults < config.json["watchdog"] < env.  Never raises."""
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads((state.bridge_home() / "config.json").read_text())
        block = raw.get("watchdog") if isinstance(raw, dict) else None
        if isinstance(block, dict):
            for k in DEFAULTS:
                if k in block:
                    cfg[k] = block[k]
    except Exception:
        pass
    # re-coerce the config.json block to the right types
    for key, default in DEFAULTS.items():
        val = cfg.get(key)
        if isinstance(default, bool):
            cfg[key] = bool(val)
        elif isinstance(default, str):
            cfg[key] = str(val or "")
        else:
            try:
                cfg[key] = float(val)
            except (TypeError, ValueError):
                cfg[key] = default
    cfg.update(_env_overrides())
    return cfg


# ---------------------------------------------------------------------------
# job["watchdog"] block (additive; never touches job.status)
# ---------------------------------------------------------------------------

FIELDS = ("state", "updated_at", "last_activity_at", "suspicion_age_s",
          "suspected_since", "snoozed_until", "diagnosis", "confirm_repeat",
          "attempts", "last_action", "recovered_at", "needs_attention_at",
          "recover_baseline_ts", "recover_deadline_ts", "tier2_strikes")


def blank_state() -> dict:
    return {"state": "healthy", "updated_at": None,
            "last_activity_at": None, "suspicion_age_s": None,
            "suspected_since": None, "snoozed_until": None,
            "diagnosis": None, "confirm_repeat": 0, "attempts": 0,
            "last_action": None, "recovered_at": None,
            "needs_attention_at": None, "recover_baseline_ts": None,
            "recover_deadline_ts": None, "tier2_strikes": 0}


def normalize_state(job: dict) -> dict:
    w = job.get("watchdog") if isinstance(job.get("watchdog"), dict) else {}
    out = blank_state()
    for k in FIELDS:
        if k in w:
            out[k] = w[k]
    if out["state"] not in WATCHDOG_STATES:
        out["state"] = "healthy"
    try:
        out["attempts"] = max(0, int(out["attempts"] or 0))
    except (TypeError, ValueError):
        out["attempts"] = 0
    try:
        out["confirm_repeat"] = max(0, int(out["confirm_repeat"] or 0))
    except (TypeError, ValueError):
        out["confirm_repeat"] = 0
    try:
        out["tier2_strikes"] = max(0, int(out["tier2_strikes"] or 0))
    except (TypeError, ValueError):
        out["tier2_strikes"] = 0
    if not isinstance(out["diagnosis"], dict):
        out["diagnosis"] = None
    if not isinstance(out["last_action"], dict):
        out["last_action"] = None
    return out


def _view(job: dict, now_ts: float | None = None) -> dict:
    """Compacted watchdog view for pi_status / pi_list / pi-lamp."""
    w = normalize_state(job)
    age = None
    if w["last_activity_at"] and now_ts is not None:
        ts = state_ts(w["last_activity_at"])
        if ts is not None:
            age = round(max(0.0, now_ts - ts), 1)
    return {
        "state": w["state"],
        "last_activity_at": w["last_activity_at"],
        "last_activity_age_s": age,
        "suspicion_age_s": w["suspicion_age_s"],
        "diagnosis_summary": (w["diagnosis"] or {}).get("summary")
        if w["diagnosis"] else None,
        "attempts": w["attempts"],
        "last_action": w["last_action"],
        "recovered_at": w["recovered_at"],
        "needs_attention_at": w["needs_attention_at"],
    }


# ---------------------------------------------------------------------------
# time + tiny local helpers  (liveness helpers intentionally mirror
# bridge.py; watchdog must stay importable from bridge WITHOUT a cycle, so
# the 10 lines are duplicated here, not shared)
# ---------------------------------------------------------------------------

def state_ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        import datetime as _dt
        return _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _unit_active(unit: str | None) -> bool:
    if not unit or not state.UNIT_RE.match(unit):
        return False
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def runner_alive(job: dict) -> bool:
    lr = job.get("last_run") or {}
    if _pid_alive(lr.get("pid")):
        return True
    if lr.get("launcher") == "systemd" and _unit_active(lr.get("unit")):
        return True
    return False


# ---------------------------------------------------------------------------
# activity evidence (read-only)
# ---------------------------------------------------------------------------

def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def session_jsonl(job: dict) -> Path | None:
    """The live Pi session file (standard store first, then legacy dir)."""
    try:
        found = runner.find_session_file(state.job_dir(job["job_id"]) / "sessions",
                                         job.get("pi_session_id") or "")
    except Exception:
        return None
    return Path(found) if found else None


def last_activity(job: dict, now: float) -> tuple[float, str]:
    """Newest local activity signal for this job and where it came from.

    The Pi session JSONL is the primary signal (Pi appends it incrementally
    as it reasons and receives tool output); turn stdout/stderr and the
    runner log are secondary; the current turn's start time is the floor.
    """
    jd = state.job_dir(job["job_id"])
    cands: list[tuple[float, str]] = []
    jf = session_jsonl(job)
    if jf is not None:
        mt = _mtime(jf)
        if mt is not None:
            cands.append((mt, "session_jsonl"))
    if job.get("pi_session_file"):
        mt = _mtime(Path(job["pi_session_file"]))
        if mt is not None:
            cands.append((mt, "session_jsonl"))
    turns = job.get("turns") or []
    if turns:
        n = turns[-1].get("n") or len(turns)
        for suffix in ("out", "err"):
            mt = _mtime(jd / "turns" / f"{n:02d}.{suffix}")
            if mt is not None:
                cands.append((mt, f"turn_{suffix}"))
        ts = state_ts(turns[-1].get("started_at"))
        if ts is not None:
            cands.append((ts, "turn_started"))
    mt = _mtime(jd / "runner.log")
    if mt is not None:
        cands.append((mt, "runner_log"))
    ts = state_ts((job.get("last_run") or {}).get("started_at"))
    if ts is not None:
        cands.append((ts, "run_started"))
    if not cands:
        return now, "none"
    return max(cands, key=lambda c: c[0])


# ---------------------------------------------------------------------------
# /proc process-tree evidence
# ---------------------------------------------------------------------------

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _boot_time() -> float:
    try:
        with open("/proc/stat", "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def read_stat(pid: int) -> dict | None:
    """{ppid, cpu_ticks, start_ticks} from /proc/<pid>/stat; None if gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode("ascii", "replace")
        rest = data[data.rindex(")") + 2:].split()
        return {"ppid": int(rest[1]),
                "cpu_ticks": int(rest[11]) + int(rest[12]),
                "start_ticks": int(rest[19])}
    except (OSError, ValueError, IndexError):
        return None


def read_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    parts = [p for p in raw.decode("utf-8", "replace").split("\0") if p]
    return " ".join(parts)[:CMDLINE_KEEP]


def _own_uid() -> int:
    return os.getuid()


def _proc_uid(pid: int) -> int | None:
    try:
        st = os.stat(f"/proc/{pid}")
        return st.st_uid
    except OSError:
        return None


def process_tree() -> dict[int, dict]:
    """Snapshot of all live same-user processes: pid -> read_stat dict."""
    out: dict[int, dict] = {}
    me = _own_uid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        st = read_stat(pid)
        if st is None:
            continue
        out[pid] = st
    # uid filter: only processes we could ever signal; foreign trees are
    # invisible to the watchdog BY DESIGN (never touch arbitrary processes).
    return {pid: st for pid, st in out.items() if _proc_uid(pid) == me}


def descendants_of(root_pid: int, tree: dict[int, dict]) -> list[int]:
    """Strict descendants of root_pid in `tree` (BFS, ancestry-verified)."""
    children: dict[int, list[int]] = {}
    for pid, st in tree.items():
        children.setdefault(st["ppid"], []).append(pid)
    out: list[int] = []
    queue = list(children.get(root_pid, []))
    seen = {root_pid}
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        queue.extend(children.get(pid, []))
    return out


def direct_tool_roots(pi_pid: int, tree: dict[int, dict]) -> list[int]:
    """Direct children of the verified Pi PID: the tool commands Pi runs.

    Tier 1 only ever interrupts these subtrees; the Pi PID itself is never
    in the set (strict children), and the runner PID is an ANCESTOR of Pi,
    so it can never appear either.
    """
    return sorted(pid for pid, st in tree.items() if st["ppid"] == pi_pid)


def verify_pi(job: dict) -> tuple[int, dict] | tuple[None, str]:
    """The current turn's Pi PID with start-time identity verified.

    Returns (pi_pid, stat) or (None, reason).  Identity is the recorded
    /proc start-time ticks (PID-reuse safe, survives cmdline rewriting).
    """
    turns = job.get("turns") or []
    if not turns:
        return None, "no-turns"
    t = turns[-1]
    pid = t.get("pi_pid")
    if not pid or not _pid_alive(pid):
        return None, "pi-pid-not-alive"
    recorded = t.get("pi_start")
    st = read_stat(pid)
    if st is None:
        return None, "pi-pid-gone"
    if recorded is not None and state.pid_start_ticks(pid) != recorded:
        return None, "pi-pid-reused"
    return pid, st


def sample_cpu(pids: list[int]) -> dict[int, int]:
    return {pid: (read_stat(pid) or {}).get("cpu_ticks", -1) for pid in pids}


def backend_activity(url: str, timeout: float = 3.0) -> dict:
    """vLLM-style /metrics probe: {reachable, running, waiting, idle} or
    {reachable: False}.  An unreachable backend is an UNKNOWN signal,
    never a confirmation and never a veto (a local gauge must not gate a
    recovery on a network hiccup -- only a *busy* backend softens it)."""
    if not url:
        return {"configured": False}
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/plain"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(256 * 1024).decode("utf-8", "replace")
    except Exception:
        return {"configured": True, "reachable": False}
    running = waiting = 0.0
    seen = False
    for line in body.splitlines():
        m = re.match(r"^vllm:num_requests_(running|waiting)\b[^ ]*\s+(\S+)", line)
        if not m:
            continue
        seen = True
        try:
            val = float(m.group(2))
        except ValueError:
            continue
        if m.group(1) == "running":
            running += val
        else:
            waiting += val
    if not seen:
        return {"configured": True, "reachable": False}
    return {"configured": True, "reachable": True,
            "running": running, "waiting": waiting,
            "idle": (running + waiting) <= 0}


# ---------------------------------------------------------------------------
# diagnosis
# ---------------------------------------------------------------------------

def diagnose(job: dict, cfg: dict, now: float,
             sleep=time.sleep) -> dict:
    """Read-only multi-signal diagnosis.  Returns a diagnosis dict:

      {confirmed, tier, signals, reasons, summary}

    confirmed + tier=1: a verified descendant tool command of THIS job is
    holding the session with no CPU and no progress -> tier-1 recovery is
    allowed.  confirmed + tier=0: Pi itself is silent with nothing to
    interrupt -> report only (tier 2 is never auto-recovered).
    """
    signals: dict = {}
    reasons: list[str] = []
    turn = (job.get("turns") or [{}])[-1]
    act_ts, act_src = last_activity(job, now)
    age = now - act_ts
    signals["activity_age_s"] = round(age, 1)
    signals["activity_source"] = act_src
    signals["job_running"] = job.get("status") == "running"
    signals["runner_alive"] = runner_alive(job)

    pi, pi_info = verify_pi(job)
    signals["pi_alive"] = pi is not None
    if pi is None:
        reasons.append(f"pi-not-verified:{pi_info}")
    be = backend_activity(cfg.get("backend_metrics_url") or "")
    signals["backend"] = be

    tree = process_tree()
    roots: list[int] = []
    subtree: list[int] = []
    if pi is not None:
        roots = direct_tool_roots(pi, tree)
        subtree = descendants_of(pi, tree)
    signals["tool_roots"] = [
        {"pid": p, "cmdline": read_cmdline(p),
         "age_s": _proc_age(p, tree, now)} for p in roots]
    signals["descendant_count"] = len(subtree)

    # CPU over two samples: anything that burns CPU somewhere in the tree
    # (a real build, test run, copy, compile) is NOT a stall, no matter how
    # quiet its output.
    cpu0 = sample_cpu(subtree)
    sleep(max(0.0, float(cfg["cpu_sample_interval_s"])))
    tree2 = process_tree()
    cpu1 = sample_cpu(subtree)
    cpu_delta = 0
    for pid in subtree:
        a, b = cpu0.get(pid, 0), cpu1.get(pid, 0)
        if a < 0 or b < 0:      # process vanished mid-sample: it was ACTIVE
            cpu_delta += 1
            continue
        cpu_delta += b - a
        st2 = tree2.get(pid)
        if st2 and st2["ppid"] != (tree.get(pid) or {}).get("ppid"):
            cpu_delta += 1      # reparented: the tree is changing
    signals["cpu_delta_ticks"] = cpu_delta

    # "an old descendant command with no progress": the tool roots must
    # predate the observation window -- a command spawned mid-sample means
    # the tree is still turning over (it also failed the fresh-activity gate
    # only by a race, so it must not be killed sight unseen).
    min_age = max(float(cfg.get("descendant_min_age_s",
                                DESCENDANT_MIN_AGE_S)),
                  3.0 * float(cfg["cpu_sample_interval_s"]))
    old_roots = [p for p in roots
                 if (_proc_age(p, tree, now) or 0) >= min_age]
    signals["stale_tool_roots"] = old_roots

    confirmed = False
    tier = 0
    if not signals["job_running"]:
        reasons.append("job-not-running")
    elif not signals["runner_alive"]:
        reasons.append("runner-not-alive")
    elif pi is None:
        reasons.append("pi-not-verified")
    elif age < cfg["stall_after_s"]:
        reasons.append("activity-too-fresh")
    elif cpu_delta > 0:
        reasons.append("cpu-active")
    elif not roots:
        # nothing to interrupt: Pi itself (or its model call) is silent
        tier = 0
        confirmed = True
        reasons.append("pi-silent-no-descendants")
    elif not old_roots:
        reasons.append("descendants-just-spawned")
    else:
        confirmed = True
        tier = 1
        reasons.append("stale-descendant-zero-cpu")
        if be.get("configured") and be.get("reachable") and not be.get("idle"):
            # a busy backend is only weak counter-evidence on a SHARED
            # backend (another job may be generating); the caller requires
            # one repeat confirmation before acting on this shape.
            reasons.append("backend-busy")

    backend_busy = bool(reasons and "backend-busy" in reasons)
    summary = (f"{'+'.join(r for r in reasons if r != 'backend-busy')}"
               f"; silence={int(age)}s src={act_src}"
               f" descendants={len(subtree)} cpu_delta={cpu_delta}")
    return {"confirmed": confirmed, "tier": tier if confirmed else None,
            "backend_busy": backend_busy, "signals": signals,
            "reasons": reasons, "summary": summary[:DIAG_SUMMARY_KEEP],
            "at": state.now_iso()}


def _proc_age(pid: int, tree: dict[int, dict], now: float) -> float | None:
    st = tree.get(pid)
    if st is None:
        return None
    btime = _boot_time()
    if not btime:
        return None
    return now - (btime + st["start_ticks"] / _CLK_TCK)


# ---------------------------------------------------------------------------
# audit trail + heartbeat
# ---------------------------------------------------------------------------

def watchdog_dir() -> Path:
    return state.bridge_home() / "watchdog"


def pending_dir() -> Path:
    return watchdog_dir() / "pending"


def events_dir() -> Path:
    return watchdog_dir() / "events"


def audit(job_id: str, event: str, details: dict) -> None:
    """Append-only per-job audit trail (jobs/<id>/watchdog.jsonl)."""
    try:
        jd = state.job_dir(job_id)
        line = {"ts": state.now_iso(), "event": event, "pid": os.getpid()}
        line.update(details)
        with open(jd / "watchdog.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def _write_json_atomic(path: Path, obj: dict) -> None:
    state.write_text_atomic(path, json.dumps(obj, ensure_ascii=False,
                                             indent=2, sort_keys=True))


def heartbeat() -> dict:
    try:
        return json.loads((watchdog_dir() / "heartbeat.json").read_text())
    except Exception:
        return {}


def _save_heartbeat(summary: dict) -> None:
    try:
        watchdog_dir().mkdir(parents=True, exist_ok=True)
        hb = {"version": VERSION, "pid": os.getpid(),
              "last_tick_at": state.now_iso(), **summary}
        _write_json_atomic(watchdog_dir() / "heartbeat.json", hb)
    except OSError:
        pass


def _tick_lock():
    """Non-blocking exclusive lock: one tick at a time (never re-entrant)."""
    watchdog_dir().mkdir(parents=True, exist_ok=True)
    f = open(watchdog_dir() / "tick.lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


# ---------------------------------------------------------------------------
# stall events -> Hermes (durable spool, separate event type + route)
# ---------------------------------------------------------------------------

def stall_url() -> str | None:
    """The stall-route URL, derived from the wake config (same platform).

    wake.json stores .../webhooks/pi-bridge-complete; the stall route is a
    sibling on the same host/port.  None when wake is disabled.
    """
    cfg = wake.load_config()
    if not cfg["enabled"]:
        return None
    url = cfg["url"]
    parts = urlsplit(url)
    path = parts.path or ""
    if path.rstrip("/").endswith(hermes_setup_route_suffix()):
        path = path[: -len(hermes_setup_route_suffix())] + STALL_ROUTE_NAME
    else:
        # foreign wake url shape: append the stall route under /webhooks/
        path = "/webhooks/" + STALL_ROUTE_NAME
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def hermes_setup_route_suffix() -> str:
    from . import hermes_setup
    return hermes_setup.ROUTE_NAME


def event_payload(job: dict, wd: dict, severity: str) -> dict:
    turn_n = len(job.get("turns") or [])
    origin = job.get("origin")
    payload = {
        "event": STALL_EVENT,
        "event_type": STALL_EVENT,
        "severity": severity,
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "turn": turn_n,
        "cwd": job.get("cwd"),
        "task_preview": (job.get("task") or "")[:200],
        "pi_session_id": job.get("pi_session_id"),
        "watchdog": _view(job, time.time()),
        "diagnosis": (wd.get("diagnosis") or {}).get("summary"),
        "diagnosis_detail": (wd.get("diagnosis") or {}).get("reasons"),
        "recovery_attempts": wd.get("attempts"),
        "last_recovery_action": wd.get("last_action"),
        "created_at": state.now_iso(),
    }
    if origin:
        payload["origin"] = origin
    return payload


def queue_event(job_id: str, severity: str) -> dict | None:
    """Durably spool one stall event; delivered (and retried) by later ticks.

    The event id is CONSTANT for this (job, turn, severity) so the
    gateway's idempotency cache collapses duplicates into one agent run --
    the same X-Request-ID trick the completion wake uses.
    """
    url = stall_url()
    if url is None:
        return None
    try:
        job = state.load_job(job_id)
    except BridgeError:
        return None
    wd = normalize_state(job)
    turn_n = len(job.get("turns") or [])
    event_id = f"{job_id}-t{turn_n}-{severity}"
    rec = {"id": event_id, "url": url, "payload": event_payload(job, wd, severity),
           "created_at": state.now_iso(), "attempts": 0, "last_error": None}
    pending_dir().mkdir(parents=True, exist_ok=True)
    path = pending_dir() / f"{event_id}.json"
    if path.exists():
        return rec  # already spooled; the retry loop owns it now
    _write_json_atomic(path, rec)
    audit(job_id, f"event-queued:{severity}", {"event_id": event_id})
    return rec


def _post_event(rec: dict) -> tuple[bool, str | None]:
    """One signed POST attempt (wake's HMAC V2 scheme, fresh timestamp).

    http.client on purpose (exactly like wake.post_once): the header names
    must go out VERBATIM -- urllib rewrites their case and the gateway's
    X-Request-ID idempotency cache would miss the header.
    """
    secret = wake.load_config()["secret"]
    if not secret:
        return False, "no-secret"
    body = json.dumps(rec["payload"], ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "X-Webhook-Timestamp": ts,
               "X-Webhook-Signature-V2": wake.sign(secret, body, ts),
               "X-Request-ID": rec["id"]}
    conn = None
    try:
        conn, target = wake._connect(rec["url"], 15.0)
        conn.request("POST", target, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp.read(4096)
    except HTTPException as e:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return False, f"bad response: {type(e).__name__}"[:200]
    except (OSError, ValueError) as e:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return False, (str(e) or type(e).__name__)[:200]
    except Exception as e:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return False, type(e).__name__[:200]
    try:
        conn.close()
    except Exception:
        pass
    if 200 <= status < 300:
        return True, f"HTTP {status}"
    return False, f"HTTP {status}"


def flush_events(cfg: dict) -> dict:
    """Retry every spooled stall event; expire after event_expire_s."""
    stats = {"delivered": 0, "retry": 0, "expired": 0}
    try:
        files = sorted(pending_dir().glob("*.json"))
    except OSError:
        return stats
    expire = float(cfg["event_expire_s"])
    events_dir().mkdir(parents=True, exist_ok=True)
    wake_on = wake.is_enabled()
    for path in files:
        try:
            rec = json.loads(path.read_text())
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        created = state_ts(rec.get("created_at")) or time.time()
        if not wake_on:
            # the wake channel is off: the stall route cannot exist; keep
            # the event as audit evidence only, do not spin on it
            rec["status"] = "not-delivered:wake-disabled"
            _write_json_atomic(events_dir() / path.name, rec)
            path.unlink(missing_ok=True)
            stats["expired"] += 1
            continue
        ok, note = _post_event(rec)
        if ok:
            rec["status"] = "delivered"
            _write_json_atomic(events_dir() / path.name, rec)
            path.unlink(missing_ok=True)
            stats["delivered"] += 1
            continue
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
        rec["last_error"] = note
        if time.time() - created > expire:
            rec["status"] = "expired"
            _write_json_atomic(events_dir() / path.name, rec)
            path.unlink(missing_ok=True)
            stats["expired"] += 1
            try:
                audit(rec["payload"].get("job_id", ""),
                      "event-expired", {"event_id": rec["id"]})
            except Exception:
                pass
        else:
            _write_json_atomic(path, rec)
            stats["retry"] += 1
    return stats


# ---------------------------------------------------------------------------
# user message on needs_attention (origin channel, messaging platforms only)
# ---------------------------------------------------------------------------

def user_attention_text(job: dict, wd: dict) -> str:
    summary = (wd.get("diagnosis") or {}).get("summary") or "stall confirmed"
    return ("Pi-задача {}: WATCHDOG — зависание, автовосстановление не "
            "помогло ({} попыток). Диагностика: {}. Требуется внимание "
            "человека: pi_status {}, job остаётся running."
            .format(job.get("job_id"), wd.get("attempts"),
                    " ".join(summary.split())[:200], job.get("job_id")))[:1100]


def notify_user_attention(job_id: str) -> dict:
    """Direct origin-channel message that the job needs a human (bounded,
    no retry, never raises).  webui origin: no direct injection (the
    native completion-event pipeline is completion-only); the stall wake
    route and pi-lamp carry that case."""
    try:
        job = state.load_job(job_id)
        cfg = deliver.load_config()
        if not cfg["enabled"]:
            return {"ok": False, "reason": "disabled"}
        text = user_attention_text(job, normalize_state(job))
        hermes = deliver.discover_hermes_bin(cfg)
        p = deliver.plan(job, hermes, text)
        wd = normalize_state(job)
        result = {"attempted": True, "ok": False, "channel": p["channel"],
                  "reason": p["reason"] or "not-supported", "at": state.now_iso()}
        if p["kind"] == "send" and p["argv"]:
            try:
                rc, out, err = deliver._run_bounded(p["argv"], min(60.0, cfg["timeout"]))
                result["ok"] = (rc == 0)
                result["reason"] = "ok" if rc == 0 else "cli-failed"
            except deliver._DeliveryTimeout:
                result["reason"] = "timeout"
            except OSError as e:
                result["reason"] = f"not-runnable: {e}"[:120]
        elif p["kind"] == "webui-event":
            result["reason"] = "webui-via-wake-route"
        with state.job_lock(job_id):
            fresh = state.load_job(job_id)
            wd2 = normalize_state(fresh)
            wd2["last_action"] = dict(wd2.get("last_action") or {})
            wd2["last_action"]["user_message"] = result
            fresh["watchdog"] = wd2
            state.save_job(fresh)
        audit(job_id, "user-attention-message", result)
        return result
    except Exception as e:
        return {"ok": False, "reason": f"internal:{type(e).__name__}"}


# ---------------------------------------------------------------------------
# tier-1 recovery
# ---------------------------------------------------------------------------

def recover(job_id: str, trigger: str = "manual") -> dict:
    """Tier-1: interrupt ONLY verified descendant tool commands of this job.

    Never kills Pi.  Never kills the runner.  Never touches a process that
    is not a same-user descendant of THIS job's identity-verified Pi PID.
    SIGTERM first, bounded grace, then SIGKILL of the survivors only.
    Structured result + audit line; idempotent (a second call just re-
    verifies and normally finds nothing left to kill).
    """
    with state.job_lock(job_id):
        job = state.load_job(job_id)
        if job["status"] not in state.ACTIVE_STATUSES:
            return {"ok": False, "reason": "job-not-active",
                    "status": job.get("status")}
        if job.get("cancel_requested"):
            return {"ok": False, "reason": "cancel-in-progress"}
        pi, info = verify_pi(job)
        if pi is None:
            return {"ok": False, "reason": f"pi-not-verified:{info}"}
        turn_n = (job.get("turns") or [{}])[-1].get("n")
        tree = process_tree()
        roots = direct_tool_roots(pi, tree)
        targets = []
        for pid in descendants_of(pi, tree):
            st = tree.get(pid)
            if st is None:
                continue
            targets.append({"pid": pid, "start": st["start_ticks"],
                            "cmdline": read_cmdline(pid)})
    if not targets:
        audit(job_id, "recover-nothing", {"trigger": trigger,
                                          "reason": "no-verified-descendants"})
        return {"ok": False, "reason": "no-verified-descendants"}
    # baseline for the "did the session resume?" confirmation, taken BEFORE
    # signalling (the confirmation is `activity > this`, not timer-based)
    pre_baseline, _ = last_activity(job, time.time())

    # signal phase OUTSIDE the lock; every signal re-verifies the exact
    # pid identity (start ticks) and that the job is still active.
    term_sent, skipped = [], []
    for t in targets:
        if not _job_still_running(job_id, turn_n):
            return {"ok": False, "reason": "job-changed", "terminated": term_sent}
        if state.pid_start_ticks(t["pid"]) != t["start"]:
            skipped.append({**t, "why": "pid-reused"})
            continue
        if t["pid"] == pi:
            skipped.append({**t, "why": "pi-self"})   # defensive; never in set
            continue
        try:
            os.kill(t["pid"], signal.SIGTERM)
            term_sent.append(t)
        except ProcessLookupError:
            pass
        except PermissionError:
            skipped.append({**t, "why": "not-ours"})
    grace = 0.0
    try:
        grace = float(load_config()["term_grace_s"])
    except Exception:
        grace = 5.0
    deadline = time.monotonic() + grace
    survivors = []
    while time.monotonic() < deadline:
        survivors = [t for t in term_sent
                     if state.pid_start_ticks(t["pid"]) == t["start"]]
        if not survivors:
            break
        time.sleep(0.25)
    killed = []
    for t in survivors:
        if not _job_still_running(job_id, turn_n):
            return {"ok": False, "reason": "job-changed", "terminated": term_sent}
        if state.pid_start_ticks(t["pid"]) != t["start"]:
            continue
        try:
            os.kill(t["pid"], signal.SIGKILL)
            killed.append(t)
        except (ProcessLookupError, PermissionError):
            pass
    result = {"ok": True, "trigger": trigger, "pi_pid": pi,
              "terminated": [{"pid": t["pid"], "cmdline": t["cmdline"]}
                             for t in term_sent],
              "killed": [{"pid": t["pid"]} for t in killed],
              "skipped": skipped, "at": state.now_iso()}
    audit(job_id, "recover-tier1", result)

    # record the action + enter `recovering` so the watchdog (and pi-lamp /
    # pi_status) can CONFIRM the resume instead of assuming it -- also for
    # a manual `pi-bridge recover`.  A manual attempt does not consume the
    # watchdog's automatic attempt budget.
    try:
        grace = float(load_config()["confirm_grace_s"])
    except Exception:
        grace = 180.0

    def _mark(wd, j):
        if j["status"] not in state.ACTIVE_STATUSES:
            return False
        wd["state"] = "recovering"
        wd["last_action"] = {"at": state.now_iso(), "trigger": trigger,
                             "ok": True, "terminated": result["terminated"],
                             "killed": result["killed"]}
        wd["recover_baseline_ts"] = pre_baseline
        wd["recover_deadline_ts"] = time.time() + grace
        if trigger != "cli" and not str(trigger).startswith("watchdog:"):
            wd["attempts"] = wd["attempts"] + 1
    _mutate(job_id, _mark)
    return result


def _job_still_running(job_id: str, turn_n: int | None) -> bool:
    try:
        job = state.load_job(job_id)
    except BridgeError:
        return False
    if job["status"] not in state.ACTIVE_STATUSES or job.get("cancel_requested"):
        return False
    turns = job.get("turns") or []
    return bool(turns) and turns[-1].get("n") == turn_n


# ---------------------------------------------------------------------------
# the tick
# ---------------------------------------------------------------------------

def _mutate(job_id: str, fn) -> dict | None:
    """Read-modify-write the watchdog block under the job lock.
    fn(wd, job) may update wd; returning False skips the save."""
    try:
        with state.job_lock(job_id):
            job = state.load_job(job_id)
            wd = normalize_state(job)
            if fn(wd, job) is False:
                return job
            wd["updated_at"] = state.now_iso()
            job["watchdog"] = wd
            state.save_job(job)
            return job
    except Exception:
        return None


def _set(job_id: str, **fields) -> None:
    def fn(wd, job):
        wd.update(fields)
    _mutate(job_id, fn)


def _check_recovery(job: dict, cfg: dict, now: float) -> str:
    """While recovering: did activity resume?  Returns the new state."""
    wd = normalize_state(job)
    act_ts, _ = last_activity(job, now)
    baseline = wd.get("recover_baseline_ts")
    if baseline is not None and act_ts > float(baseline):
        _set(job["job_id"], state="recovered", recovered_at=state.now_iso(),
             suspicion_age_s=None, snoozed_until=None,
             recover_baseline_ts=None, recover_deadline_ts=None)
        audit(job["job_id"], "recovered",
              {"after_recovery_action": wd.get("attempts")})
        return "recovered"
    deadline = wd.get("recover_deadline_ts")
    if deadline is not None and now > float(deadline):
        if wd["attempts"] >= int(cfg["max_recovery_attempts"]):
            _escalate(job["job_id"], cfg, "confirm-timeout")
            return "needs_attention"
        # recovery did not take: another (final) tier-1 cycle
        _recover_cycle(job["job_id"], cfg, reason="confirm-timeout")
        return "recovering"
    return "recovering"


def _recover_cycle(job_id: str, cfg: dict, reason: str) -> dict:
    """One tier-1 recovery cycle: mark recovering, queue the Hermes stall
    event BEFORE acting, run the recovery, then set the confirm window."""
    act_ts, _ = last_activity(state.load_job(job_id), time.time())
    _set(job_id, state="recovering",
         recover_baseline_ts=act_ts,
         recover_deadline_ts=time.time() + float(cfg["confirm_grace_s"]))
    queue_event(job_id, "stall")
    result = recover(job_id, trigger=f"watchdog:{reason}")

    def fn(wd, job):
        wd["attempts"] = wd["attempts"] + 1
        action = {"at": state.now_iso(), "trigger": f"watchdog:{reason}",
                  "ok": result.get("ok"), "terminated": result.get("terminated"),
                  "killed": result.get("killed"),
                  "reason": result.get("reason")}
        wd["last_action"] = action
        # re-arm the confirm window from the moment the signal was sent
        wd["recover_deadline_ts"] = time.time() + float(cfg["confirm_grace_s"])
    _mutate(job_id, fn)
    return result


def _escalate(job_id: str, cfg: dict, reason: str) -> None:
    _set(job_id, state="needs_attention",
         needs_attention_at=state.now_iso())
    audit(job_id, "needs-attention", {"reason": reason})
    queue_event(job_id, "attention")
    notify_user_attention(job_id)


def tick_job(job: dict, cfg: dict, now: float, sleep=time.sleep) -> dict:
    """One watchdog evaluation of one ACTIVE job.  Never changes job.status."""
    job_id = job["job_id"]
    turns = job.get("turns") or []
    if not turns or turns[-1].get("finished_at"):
        return {"job_id": job_id, "action": "skip-turn-done"}
    wd = normalize_state(job)
    act_ts, src = last_activity(job, now)
    age = now - act_ts
    started = state_ts(turns[-1].get("started_at")) or 0.0
    startup_grace = float(cfg["startup_grace_s"])

    def _record_activity():
        _set(job_id, last_activity_at=time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(act_ts)))

    if age < cfg["stall_after_s"]:
        # normal, working (or freshly started) job
        if wd["state"] in ("suspected_stall", "diagnosing"):
            _set(job_id, state="healthy", suspicion_age_s=None,
                 snoozed_until=None, confirm_repeat=0, tier2_strikes=0,
                 last_activity_at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime(act_ts)))
            audit(job_id, "cleared", {"silence_s": int(age)})
        elif wd["state"] == "needs_attention":
            # activity resumed on its own after we gave up
            if wd.get("needs_attention_at"):
                nat = state_ts(wd["needs_attention_at"]) or 0.0
                if act_ts > nat:
                    _set(job_id, state="recovered", recovered_at=state.now_iso(),
                         last_activity_at=time.strftime(
                             "%Y-%m-%dT%H:%M:%SZ", time.gmtime(act_ts)))
                    audit(job_id, "resumed-spontaneously", {})
                    return {"job_id": job_id, "action": "recovered"}
            _record_activity()
        elif wd["state"] == "recovering":
            pass  # handled below; recovering must be CONFIRMED, not assumed
        else:
            _record_activity()
        if wd["state"] != "recovering" and age < startup_grace and now - started < startup_grace:
            return {"job_id": job_id, "action": "startup-grace"}
        if wd["state"] != "recovering":
            return {"job_id": job_id, "action": "healthy"}

    if wd["state"] == "recovering":
        new_state = _check_recovery(job, cfg, now)
        return {"job_id": job_id, "action": new_state}

    if wd["state"] == "needs_attention":
        # already escalated; keep quiet, the human is the only one left
        return {"job_id": job_id, "action": "needs-attention"}

    if wd.get("snoozed_until"):
        su = state_ts(wd["snoozed_until"]) or 0.0
        if now < su:
            return {"job_id": job_id, "action": "snoozed"}

    # silent beyond the threshold: suspect first, then diagnose
    if wd["state"] in ("healthy", "recovered"):
        _set(job_id, state="suspected_stall",
             suspected_since=state.now_iso(),
             suspicion_age_s=int(age),
             last_activity_at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime(act_ts)))
        audit(job_id, "suspected", {"silence_s": int(age), "source": src})
    _set(job_id, state="diagnosing")
    diag = diagnose(job, cfg, now, sleep=sleep)

    if not diag["confirmed"]:
        snooze = float(cfg["stall_after_s"])
        _set(job_id, state="suspected_stall", diagnosis=diag,
             confirm_repeat=0, snoozed_until=time.strftime(
                 "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + snooze)))
        audit(job_id, "insufficient-evidence",
              {"reasons": diag["reasons"], "silence_s": int(age)})
        return {"job_id": job_id, "action": "snoozed",
                "reasons": diag["reasons"]}

    if diag["tier"] == 0:
        # tier-2 shape: Pi itself silent, nothing to interrupt.  NEVER
        # auto-recovered; repeated identical strikes -> needs_attention
        # immediately (a recovery attempt is not even possible here).
        strikes = wd["tier2_strikes"] + 1
        if strikes >= TIER2_REPEAT_BEFORE_ATTENTION:
            _set(job_id, diagnosis=diag, tier2_strikes=strikes)
            _escalate(job_id, cfg, "pi-silent-no-recovery-left")
            return {"job_id": job_id, "action": "needs-attention"}
        _set(job_id, state="suspected_stall", diagnosis=diag,
             tier2_strikes=strikes, confirm_repeat=0,
             snoozed_until=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(now + 60.0)))
        audit(job_id, "tier2-suspected", {"strikes": strikes,
                                          "summary": diag["summary"]})
        return {"job_id": job_id, "action": "tier2-suspected"}

    if diag["backend_busy"] and wd["confirm_repeat"] < 1:
        # one shared-backend busy reading: demand a second confirmation
        _set(job_id, state="suspected_stall", diagnosis=diag,
             confirm_repeat=wd["confirm_repeat"] + 1,
             snoozed_until=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(now + 60.0)))
        audit(job_id, "confirm-deferred", {"why": "backend-busy"})
        return {"job_id": job_id, "action": "confirm-deferred"}

    if wd["attempts"] >= int(cfg["max_recovery_attempts"]):
        _set(job_id, diagnosis=diag)
        _escalate(job_id, cfg, "confirmed-stall")
        return {"job_id": job_id, "action": "needs-attention"}

    audit(job_id, "stall-confirmed", {"summary": diag["summary"],
                                      "attempts": wd["attempts"]})
    _set(job_id, diagnosis=diag, confirm_repeat=0, tier2_strikes=0)
    result = _recover_cycle(job_id, cfg, reason="confirmed-stall")
    return {"job_id": job_id, "action": "recovered-attempt",
            "recover": result}


def tick() -> dict:
    """One watchdog tick (the timer's ExecStart).  Never raises."""
    lock = _tick_lock()
    if lock is None:
        return {"skipped": "another-tick-running"}
    try:
        return _tick()
    except Exception as e:  # the timer must never see a crash loop
        _save_heartbeat({"last_error": f"{type(e).__name__}: {e}"[:300]})
        return {"error": str(e)[:300]}
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except OSError:
            pass


def _tick() -> dict:
    cfg = load_config()
    events = flush_events(cfg)
    if not cfg["enabled"]:
        _save_heartbeat({"disabled": True, "events": events})
        return {"disabled": True, "events": events}
    now = time.time()
    counts: dict[str, int] = {}
    actions: dict[str, int] = {}
    scanned = 0
    for job in state.list_jobs():
        if job.get("status") not in state.ACTIVE_STATUSES:
            continue  # terminal jobs belong to the completion pipeline
        # cheap pre-filter: never sample a job whose runner is gone
        if not runner_alive(job):
            continue  # reconcile() (status path) owns dead-runner jobs
        scanned += 1
        try:
            fresh = state.load_job(job["job_id"])
            res = tick_job(fresh, cfg, now)
        except Exception as e:
            audit(job.get("job_id", ""), "tick-error",
                  {"error": f"{type(e).__name__}: {e}"[:200]})
            continue
        counts[normalize_state(fresh)["state"]] = \
            counts.get(normalize_state(fresh)["state"], 0) + 1
        a = res.get("action", "?")
        actions[a] = actions.get(a, 0) + 1
    _save_heartbeat({"events": events, "scanned": scanned, "states": counts,
                     "actions": actions})
    return {"scanned": scanned, "states": counts, "actions": actions,
            "events": events}


# ---------------------------------------------------------------------------
# read-only status view (`pi-bridge watchdog status`)
# ---------------------------------------------------------------------------

def status_view() -> dict:
    hb = heartbeat()
    jobs = []
    for job in state.list_jobs():
        if job.get("status") not in state.ACTIVE_STATUSES:
            continue
        v = _view(job, time.time())
        v["job_id"] = job["job_id"]
        v["job_status"] = job["status"]
        jobs.append(v)
    pending = []
    try:
        for p in sorted(pending_dir().glob("*.json")):
            try:
                rec = json.loads(p.read_text())
                pending.append({"id": rec.get("id"),
                                "attempts": rec.get("attempts"),
                                "created_at": rec.get("created_at"),
                                "last_error": rec.get("last_error")})
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return {"watchdog": hb, "config": load_config(),
            "stall_url": stall_url(), "jobs": jobs, "pending_events": pending}


# `_view` is private; expose it for bridge.status_view / list_rows.
def job_view(job: dict, now_ts: float | None = None) -> dict:
    return _view(job, now_ts if now_ts is not None else time.time())
