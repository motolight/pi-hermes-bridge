"""Core bridge logic: submit / feedback / cancel / status.

Used by the CLI and (partly) by the runner.  No shell anywhere; tasks are
delivered to pi via stdin by pi_bridge.runner.

Concurrency protocol:
- job.json is written atomically (tmp + os.replace) at all times.
- All read-modify-write transitions take state.job_lock(job_id) (flock).
  Writers: CLI (submit/feedback/cancel/reconcile) and the runner.
- The runner is the writer of terminal statuses except that cancel may force
  `cancelled` under the lock when the runner is verifiably gone/dead.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import piweb, state, wake
from .state import BridgeError

# Max chars of the final result kept inside job.json / returned by status.
RESULT_KEEP = 8000           # in job.json
RESULT_LIMIT_DEFAULT = 4000  # status output (override: PI_BRIDGE_RESULT_LIMIT)
ERROR_KEEP = 2000
TASK_PREVIEW = 200
MAX_TASK_CHARS = 200_000
ENV_SNAPSHOT_KEYS = ("PATH", "HOME", "LANG", "LC_ALL")

# --------------------------------------------------------------------------
# Origin capture (V1.3): where the delegation request came from, so a woken
# Hermes run can deliver the result back to the *right* channel.  The bridge
# stores this and echoes it in status/list; it NEVER interprets it, routes on
# it or delivers anything itself.  Validation drops malformed values silently
# (origin is best-effort metadata, never a hard input error).
# --------------------------------------------------------------------------
ORIGIN_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
ORIGIN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def clean_origin(platform: str = "", chat_id: str = "",
                 thread_id: str = "", ui_session_id: str = "") -> dict:
    """Sanitize the four fixed origin fields; invalid values are dropped.

    Sanitizes with fullmatch semantics (a plain `$` would also accept a
    trailing newline).  Returns a dict with only the valid, non-empty keys
    (possibly empty).  No other keys ever exist: the schema is a stable,
    fixed set of four.
    """
    origin = {}
    if platform and ORIGIN_PLATFORM_RE.fullmatch(platform):
        origin["platform"] = platform
    for key, val in (("chat_id", chat_id), ("thread_id", thread_id),
                     ("ui_session_id", ui_session_id)):
        if val and ORIGIN_ID_RE.fullmatch(val):
            origin[key] = val
    return origin


def _result_limit() -> int:
    try:
        return max(200, int(os.environ.get("PI_BRIDGE_RESULT_LIMIT", "")))
    except ValueError:
        return RESULT_LIMIT_DEFAULT


def _piweb_enabled() -> bool:
    return (os.environ.get("PI_BRIDGE_NO_PIWEB", "") or "").lower() not in (
        "1", "true", "yes", "on")


def _piweb_budget() -> float:
    try:
        return max(0.2, float(os.environ.get("PI_BRIDGE_PIWEB_BUDGET", "")))
    except ValueError:
        return piweb.DEFAULT_BUDGET


def piweb_view(job: dict) -> dict:
    """Optional read-only PI WEB view of a job.

    PI WEB is an enhancement, never a dependency: any failure (unreachable,
    slow, misconfigured, internal bug here) degrades to
    {"available": false, ...} and can never change or break the job status.
    """
    if not _piweb_enabled():
        return {"available": False, "reason": "disabled-by-env"}
    try:
        return piweb.observe_job(job, budget_seconds=_piweb_budget())
    except Exception as e:  # piweb already swallows; this is a second net
        return {"available": False, "reason": f"internal: {type(e).__name__}"}


def bridge_config() -> dict:
    """Operator-level bridge config: <bridge_home>/config.json (written by `pi-bridge install`).
    Malformed/missing -> {}."""
    import json
    path = state.bridge_home() / "config.json"
    try:
        cfg = json.loads(path.read_text())
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def discover_pi_bin() -> str:
    """Portable Pi executable discovery, in strict priority order:
    1. PI_BRIDGE_PI_BIN env (explicit override);
    2. pi_bin recorded by `pi-bridge install` in <bridge_home>/config.json
       (installer ran `command -v pi` + `pi --version` + capability check);
    3. shutil.which("pi") in the current PATH;
    4. empty string -> caller raises with instructions (no silent fallback)."""
    env = (os.environ.get("PI_BRIDGE_PI_BIN") or "").strip()
    if env:
        return env
    saved = str(bridge_config().get("pi_bin") or "").strip()
    if saved:
        return saved
    return shutil.which("pi") or ""


def check_pi_capability(pi_path: str) -> str:
    """Verify the binary answers `pi --version` and supports the print-mode flags the
    bridge needs (--print, --agent, --session-id, --session-dir).
    Returns version string; raises BridgeError with a clear message otherwise."""
    import subprocess
    try:
        v = subprocess.run([pi_path, "--version"], capture_output=True, text=True, timeout=15)
    except OSError as e:
        raise BridgeError(f"pi binary not runnable: {pi_path}: {e}")
    if v.returncode != 0:
        raise BridgeError(f"`pi --version` failed (rc={v.returncode}): {(v.stderr or v.stdout).strip()[:200]}")
    try:
        h = subprocess.run([pi_path, "--help"], capture_output=True, text=True, timeout=15)
        help_text = h.stdout + h.stderr
    except OSError:
        help_text = ""
    missing = [f for f in ("--print", "--agent", "--session-id", "--session-dir") if f not in help_text]
    if missing:
        hint = (" NOTE: --agent is provided by the pi-open-agents extension (or an "
                "equivalent), not by pi core; configure it in the pi agent home "
                "first (docs/PI_ORCHESTRATOR_EXAMPLE.md)."
                if "--agent" in missing else "")
        raise BridgeError(f"pi at {pi_path} lacks required flags: {', '.join(missing)} "
                          "(bridge needs pi print-mode with --agent/--session-id)." + hint)
    return (v.stdout or "").strip().splitlines()[0] if (v.stdout or v.stderr).strip() else "unknown"


def resolve_pi_bin(pi_bin: str | None) -> str:
    candidate = (pi_bin or "").strip() or discover_pi_bin()
    if not candidate:
        raise BridgeError(
            "pi executable not found. Fix one of: (a) put `pi` on PATH; "
            "(b) export PI_BRIDGE_PI_BIN=/abs/path/to/pi; "
            "(c) run `pi-bridge install` (records the absolute path + capability check "
            f"in {state.bridge_home() / 'config.json'}).")
    p = Path(candidate).expanduser()
    if not p.is_absolute():
        found = shutil.which(str(p))
        if not found:
            raise BridgeError(f"pi executable not found: {candidate}")
        p = Path(found)
    if not p.is_file() or not os.access(p, os.X_OK):
        raise BridgeError(f"pi executable not usable: {p}")
    return str(p.resolve())


def validate_cwd(cwd: str) -> str:
    p = Path(cwd).expanduser()
    if not p.exists():
        raise BridgeError(f"cwd does not exist: {cwd}")
    p = p.resolve()
    if not p.is_dir():
        raise BridgeError(f"cwd is not a directory: {cwd}")
    return str(p)


def _env_snapshot() -> dict:
    snap = {}
    for k in ENV_SNAPSHOT_KEYS:
        v = os.environ.get(k)
        if v and "\n" not in v and "\x00" not in v:
            snap[k] = v
    return snap


def _launcher_mode() -> str:
    mode = os.environ.get("PI_BRIDGE_LAUNCHER", "auto")
    if mode not in ("auto", "systemd", "detach"):
        raise BridgeError(f"invalid PI_BRIDGE_LAUNCHER: {mode}")
    return mode


def _unit_name(job_id: str, turn: int) -> str:
    name = f"pi-bridge-{job_id}-t{turn}"
    if not state.UNIT_RE.match(name):
        raise BridgeError("internal error: invalid unit name", code=1)
    return name


# A transient systemd unit starts with a clean environment: without this the
# PI_BRIDGE_* knobs (wake cadence, piweb budget, result limit) would be
# silently ignored in systemd mode while working in detach mode.  Config
# files (wake.json) are unaffected; nothing secret is ever forwarded.
FORWARD_ENV_KEYS = (
    "PI_BRIDGE_WAKE_INTERVAL", "PI_BRIDGE_WAKE_MAX_ATTEMPTS",
    "PI_BRIDGE_WAKE_PERMANENT_AFTER", "PI_BRIDGE_WAKE_HTTP_TIMEOUT",
    "PI_BRIDGE_WAKE_PIWEB_BUDGET", "PI_BRIDGE_NO_PIWEB",
    "PI_BRIDGE_PIWEB_BUDGET", "PI_BRIDGE_RESULT_LIMIT",
    "PI_WEB_URL", "PI_WEB_CONFIG",
)


def _forwarded_env_args() -> list[str]:
    args = []
    for k in FORWARD_ENV_KEYS:
        v = os.environ.get(k)
        if v and "\n" not in v and "\r" not in v and "\x00" not in v:
            args += ["--setenv", f"{k}={v}"]
    return args


def _systemd_run_available() -> bool:
    if shutil.which("systemd-run") is None:
        return False
    if not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", "--property=DefaultTarget"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _plan_launcher() -> str:
    mode = _launcher_mode()
    if mode == "auto":
        return "systemd" if _systemd_run_available() else "detach"
    if mode == "systemd" and not _systemd_run_available():
        raise BridgeError("PI_BRIDGE_LAUNCHER=systemd but systemd-run --user unavailable")
    return mode


def _runner_argv(job_id: str, turn: int) -> list[str]:
    return [sys.executable, "-m", "pi_bridge.runner", job_id,
            "--turn", str(turn), "--home", str(state.bridge_home())]


def _spawn_detached(job_id: str, turn: int) -> int:
    jd = state.job_dir(job_id)
    log = open(jd / "runner.log", "ab")
    try:
        env = os.environ.copy()
        env["PI_BRIDGE_HOME"] = str(state.bridge_home())
        proc = subprocess.Popen(
            _runner_argv(job_id, turn),
            cwd=str(jd),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,  # own session; we are allowed to killpg it
            env=env,
        )
    finally:
        log.close()
    return proc.pid


def _launch_runner(job_id: str, turn: int, anchor: dict) -> dict:
    """Start the runner for a turn durably. Returns the final last_run dict.

    `anchor` is the pre-launch last_run (launcher/unit/started_at) already
    persisted in job.json, so liveness checks work even before this returns.
    """
    jd = state.job_dir(job_id)
    if anchor["launcher"] == "systemd":
        cmd = [
            "systemd-run", "--user", "--collect", "--quiet",
            "--unit", anchor["unit"],
            "--property", "Restart=no",
            # The runner may legitimately outlive the pi process: after a
            # terminal turn it keeps running while the wake notifier retries
            # delivery (up to ~45 min).  A start timeout would SIGKILL it
            # mid-notification and Hermes would never be woken.
            "--property", "TimeoutStartSec=infinity",
            "--property", "TimeoutStopSec=30s",
            "--working-directory", str(jd),
        ] + _forwarded_env_args() + _runner_argv(job_id, turn)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            return dict(anchor)
        if _launcher_mode() == "systemd":
            raise BridgeError(
                f"systemd-run failed: {(r.stderr or r.stdout).strip()[:400]}"
            )
        # auto mode with pre-saved systemd anchor: fall back to detach.
        pid = _spawn_detached(job_id, turn)
        return {"launcher": "detach", "unit": None, "pid": pid,
                "started_at": anchor["started_at"]}
    pid = _spawn_detached(job_id, turn)
    return {"launcher": "detach", "unit": None, "pid": pid,
            "started_at": anchor["started_at"]}


def _post_launch_merge(job_id: str, launched: dict) -> dict:
    with state.job_lock(job_id):
        fresh = state.load_job(job_id)
        merged = dict(fresh.get("last_run") or {})
        if not merged.get("pid") and launched.get("pid"):
            merged["pid"] = launched["pid"]
        merged["launcher"] = launched["launcher"]
        merged["unit"] = launched["unit"]
        fresh["last_run"] = merged
        state.save_job(fresh)
        return fresh


def _pre_launch_anchor(job_id: str, turn: int, launcher_kind: str) -> dict:
    anchor = {
        "launcher": launcher_kind,
        "unit": _unit_name(job_id, turn) if launcher_kind == "systemd" else None,
        "pid": None,
        "started_at": state.now_iso(),
    }
    return anchor


def _refuse_if_pi_alive(job: dict) -> None:
    """Never start a new turn while a pi process of a previous turn lives.

    Identity is the recorded /proc start-time (survives real pi rewriting
    its own cmdline via process.title); the cmdline check is only a
    fallback for legacy states without pi_start.
    """
    for t in job.get("turns") or []:
        pid = t.get("pi_pid")
        if not pid or not _pid_alive(pid):
            continue
        start = t.get("pi_start")
        if start is not None:
            if state.pid_start_ticks(pid) == start:
                raise BridgeError(
                    f"a pi process (pid {pid}) from a previous turn is still "
                    "running; wait for it to exit before sending feedback"
                )
            continue  # pid reused by an unrelated process
        if _cmdline_matches_pi(pid, job):
            raise BridgeError(
                f"a pi process (pid {pid}) from a previous turn is still "
                "running; wait for it to exit before sending feedback"
            )


def submit(task: str, cwd: str, pi_bin: str | None = None,
           session_id: str | None = None, origin: dict | None = None) -> dict:
    if not task or not task.strip():
        raise BridgeError("task is empty")
    if len(task) > MAX_TASK_CHARS:
        raise BridgeError(f"task too large ({len(task)} chars, max {MAX_TASK_CHARS})")
    real_cwd = validate_cwd(cwd)
    resolved_pi = resolve_pi_bin(pi_bin)
    if session_id is None:
        session_id = str(uuid.uuid4())
    if not state.SESSION_ID_RE.match(session_id):
        raise BridgeError(f"invalid session id: {session_id!r}")
    launcher_kind = _plan_launcher()

    job_id = state.gen_job_id()
    jd = state.job_dir(job_id)
    (jd / "sessions").mkdir(parents=True)
    (jd / "turns").mkdir()

    job = {
        "job_id": job_id,
        "created_at": state.now_iso(),
        "updated_at": state.now_iso(),
        "cwd": real_cwd,
        "status": "queued",
        "pi_session_id": session_id,
        "pi_session_file": None,
        "pi_bin": resolved_pi,
        "task": task,
        "feedbacks": [],
        "turns": [{"n": 1, "kind": "task", "started_at": state.now_iso(),
                   "finished_at": None, "exit_code": None, "pi_pid": None}],
        "last_run": _pre_launch_anchor(job_id, 1, launcher_kind),
        "cancel_requested": False,
        "env_snapshot": _env_snapshot(),
        "wake": wake.blank_state(wake.is_enabled()),
        "final_result": None,
        "final_result_chars": 0,
        "final_result_truncated": False,
        "error": None,
    }
    # Delivery metadata as captured from the requesting session (already
    # validated by clean_origin at the CLI edge). Absent when empty so old
    # job.json shapes stay byte-identical; job logic never reads this field.
    if origin:
        job["origin"] = dict(origin)
    # Full job state (including turn entry and liveness anchor) exists
    # BEFORE the runner can start.
    state.save_job(job)
    launched = _launch_runner(job_id, 1, job["last_run"])
    return _post_launch_merge(job_id, launched)


def feedback(job_id: str, text: str) -> dict:
    with state.job_lock(job_id):
        job = state.load_job(job_id)
        if job["status"] not in state.TERMINAL_STATUSES:
            raise BridgeError(
                f"job {job_id} is {job['status']}; feedback is only accepted "
                "on terminal jobs"
            )
        _refuse_if_pi_alive(job)
        if not text or not text.strip():
            raise BridgeError("feedback is empty")
        if len(text) > MAX_TASK_CHARS:
            raise BridgeError(f"feedback too large ({len(text)} chars)")
        turn = len(job["turns"]) + 1
        launcher_kind = _plan_launcher()
        job["status"] = "queued"
        job["cancel_requested"] = False
        job["feedbacks"].append({"at": state.now_iso(), "text": text})
        job["turns"].append({"n": turn, "kind": "feedback",
                             "started_at": state.now_iso(),
                             "finished_at": None, "exit_code": None,
                             "pi_pid": None})
        job["last_run"] = _pre_launch_anchor(job_id, turn, launcher_kind)
        # The wake state describes the CURRENT turn only: a new turn resets
        # the delivery bookkeeping.
        job["wake"] = wake.blank_state(wake.is_enabled())
        state.save_job(job)
    launched = _launch_runner(job_id, turn, job["last_run"])
    return _post_launch_merge(job_id, launched)


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


def _cmdline_matches_pi(pid: int, job: dict) -> bool:
    """True if pid is verifiably a pi child of THIS job (exact session id)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    return "--session-id" in cmdline and job["pi_session_id"] in cmdline


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


def _parse_iso(ts: str) -> float:
    from datetime import datetime, timezone

    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()


STARTUP_GRACE = 10  # seconds: systemd unit may still be spawning the runner


def reconcile(job: dict) -> dict:
    """Lazily reconcile crashed jobs. Persists under lock; returns fresh job."""
    if job["status"] not in state.ACTIVE_STATUSES:
        return job
    if runner_alive(job):
        return job
    started = (job.get("last_run") or {}).get("started_at")
    if started is not None and (time.time() - _parse_iso(started)) < STARTUP_GRACE:
        return job
    with state.job_lock(job["job_id"]):
        fresh = state.load_job(job["job_id"])
        if fresh["status"] not in state.ACTIVE_STATUSES or runner_alive(fresh):
            return fresh
        fresh["status"] = "cancelled" if fresh.get("cancel_requested") else "interrupted"
        if fresh["status"] == "interrupted" and not fresh.get("error"):
            fresh["error"] = "runner process disappeared without a terminal status"
        state.save_job(fresh)
        return fresh


def status_view(job: dict, full: bool = False) -> dict:
    limit = _result_limit() if not full else 10**12
    final = job.get("final_result") or ""
    shown = final if len(final) <= limit else final[:limit]
    lr = job.get("last_run") or {}
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "cwd": job["cwd"],
        "pi_session_id": job["pi_session_id"],
        "pi_session_file": job.get("pi_session_file"),
        "task_preview": (job.get("task") or "")[:TASK_PREVIEW],
        "feedback_turns": len(job.get("feedbacks") or []),
        "turns": [
            {"n": t["n"], "kind": t["kind"], "exit_code": t.get("exit_code"),
             "started_at": t.get("started_at"), "finished_at": t.get("finished_at")}
            for t in job.get("turns") or []
        ],
        "runner_alive": runner_alive(job),
        "stdout_capped": bool(job.get("stdout_capped")),
        "launcher": lr.get("launcher"),
        "unit": lr.get("unit"),
        "runner_pid": lr.get("pid"),
        "error": (job.get("error") or "")[:ERROR_KEEP] or None,
        # Wake-notifier delivery bookkeeping for the current turn (no secret).
        "wake": wake.normalize_state(job, enabled=wake.is_enabled()),
        # Delivery origin as recorded at submit time (dict or null). Metadata
        # for the woken run to route its reply; the bridge ignores it.
        "origin": job.get("origin") or None,
        "log_dir": str(state.job_dir(job["job_id"])),
        "final_result_chars": job.get("final_result_chars") or 0,
        "final_result_truncated": bool(job.get("final_result_truncated"))
        or len(final) > len(shown),
        "final_result": shown or None,
        # Optional read-only observability; never affects job semantics.
        "pi_web": piweb_view(job),
    }


# --------------------------------------------------------------------------
# listing (pi_list): compact rows, newest first, never any transcript/stderr
# --------------------------------------------------------------------------

LIST_LIMIT_DEFAULT = 20
LIST_TASK_PREVIEW = 160


def list_rows(jobs: list[dict], limit: int = LIST_LIMIT_DEFAULT) -> list[dict]:
    """Compact recovery-oriented rows, newest `updated_at` first.

    Deliberately excludes final_result, task text, transcript and stderr --
    a wake/recovery run must look at pi_status for the content of one job.
    """
    if limit is None:
        limit = LIST_LIMIT_DEFAULT
    limit = max(0, int(limit))
    live_enabled = wake.is_enabled()
    rows = []
    for job in jobs:
        w = wake.normalize_state(job, enabled=live_enabled)
        rows.append({
            "job_id": job["job_id"],
            "status": job["status"],
            "cwd": job.get("cwd"),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "task_preview": (job.get("task") or "")[:LIST_TASK_PREVIEW],
            "feedback_turns": len(job.get("feedbacks") or []),
            "turns": len(job.get("turns") or []),
            "wake": {"delivered": w["delivered"], "enabled": w["enabled"]},
            "origin": job.get("origin") or None,
        })
    rows.sort(key=lambda r: (r.get("updated_at") or "", r.get("job_id") or ""),
              reverse=True)
    return rows[:limit]


def _kill_verified_runner(pid: int, job_id: str, sig: int) -> bool:
    """Signal the runner's own process group only if its cmdline proves it is
    THIS job's runner. Returns True if the signal was delivered."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    if "pi_bridge.runner" not in cmdline or job_id not in cmdline:
        return False
    try:
        os.killpg(os.getpgid(pid), sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def cancel(job_id: str) -> dict:
    job = reconcile(state.load_job(job_id))
    if job["status"] not in state.ACTIVE_STATUSES:
        raise BridgeError(f"job {job_id} is {job['status']}; nothing to cancel")
    with state.job_lock(job_id):
        job = state.load_job(job_id)
        if job["status"] not in state.ACTIVE_STATUSES:
            raise BridgeError(f"job {job_id} is {job['status']}; nothing to cancel")
        job["cancel_requested"] = True
        state.save_job(job)

    lr = job.get("last_run") or {}
    unit = lr.get("unit")
    if unit and state.UNIT_RE.match(unit):
        # Only ever stops a bridge-owned unit name recorded in this job.
        subprocess.run(["systemctl", "--user", "stop", unit],
                       capture_output=True, text=True, timeout=30)
    term_sent = False
    pid = lr.get("pid")
    if pid and _pid_alive(pid):
        term_sent = _kill_verified_runner(pid, job_id, 0x0F)  # SIGTERM

    # Wait (outside the lock!) for the runner to finalize its own state.
    # In detach mode the runner records its pid shortly after launch: send
    # the verified SIGTERM as soon as it appears.
    deadline = time.time() + 8
    while time.time() < deadline:
        job = state.load_job(job_id)
        if job["status"] in state.TERMINAL_STATUSES:
            return job
        if not term_sent:
            pid = (job.get("last_run") or {}).get("pid")
            if pid and _pid_alive(pid):
                term_sent = _kill_verified_runner(pid, job_id, 0x0F)
        time.sleep(0.2)

    with state.job_lock(job_id):
        job = state.load_job(job_id)
        if job["status"] in state.TERMINAL_STATUSES:
            return job
        pid = (job.get("last_run") or {}).get("pid")
        if pid and _pid_alive(pid):
            _kill_verified_runner(pid, job_id, 0x09)  # SIGKILL, verified cmdline
        job["status"] = "cancelled"
        state.save_job(job)
        return job
