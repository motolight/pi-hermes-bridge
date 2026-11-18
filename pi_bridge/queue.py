"""Durable FIFO queue for Pi jobs: how many jobs may run AT THE SAME TIME.

Why this exists
---------------
Pi and Hermes share ONE inference backend.  Every concurrently running bridge
job -- and every subagent inside it -- is a separate competing request, so an
unbounded number of parallel jobs multiplies latency for all of them
(measured 2026-11: ~3x slower model answers while a nightly batch ran 8-9
jobs at once).  The queue caps how many jobs execute concurrently and makes
the rest wait, durably and in FIFO order.

Model
-----
* Every job belongs to a class: ``heavy`` (default, the pre-queue behaviour)
  or ``light`` (quick/urgent; ``pi-bridge submit --light``).  The class is
  stored in ``job["job_class"]``; jobs without the field are heavy, so every
  job.json written before the queue keeps working unchanged.
* Each class has its own slot budget (default: 1 heavy + 1 light).  A job
  only ever consumes a slot of its OWN class, which is why the queue is
  strictly FIFO *per class*: a light job never blocks a heavy one behind it
  and vice versa (a global FIFO would defeat the purpose of the extra
  interactive slot).
* A waiting job is a normal job in the existing ``queued`` status whose
  ``last_run`` anchor is still ``null`` -- i.e. no runner was ever launched
  for its current turn.  There is no second source of truth: the queue IS the
  jobs directory, so it survives a gateway/runner restart and a reboot
  without extra state to replay.
* Turning a waiting job into a running one ("admission") happens under a
  bridge-wide queue lock plus the per-job lock, and is the ONLY place that
  launches a runner.  Jobs already running when the queue was introduced are
  never touched: they keep their slot and finish normally.

Configuration (``<bridge_home>/config.json``, "queue" block; every key
optional, absent == defaults == the behaviour documented in README)::

    {"queue": {"enabled": true, "heavy_slots": 1, "light_slots": 1}}

Environment overrides (same meaning, highest priority):
``PI_BRIDGE_QUEUE=0`` disables the queue entirely -- submit then launches the
runner immediately, exactly like bridge versions <= 0.5.0;
``PI_BRIDGE_QUEUE_HEAVY_SLOTS`` / ``PI_BRIDGE_QUEUE_LIGHT_SLOTS`` set the slot
budgets (``0`` is valid and means "that class never starts").

Pumps: admission runs on submit/feedback (immediately, so an idle bridge
still starts a job in the same call), when a runner finalizes a turn, when a
job is cancelled, from the stall-watchdog systemd timer (``pi-bridge
watchdog tick`` -- the durable pump that restarts cannot stop), and manually
via ``pi-bridge queue pump``.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

from . import state
from .state import BridgeError

from datetime import datetime, timezone

CLASSES = ("heavy", "light")
DEFAULT_CONFIG = {"enabled": True, "heavy_slots": 1, "light_slots": 1}
ENV_ENABLED = "PI_BRIDGE_QUEUE"
ENV_HEAVY = "PI_BRIDGE_QUEUE_HEAVY_SLOTS"
ENV_LIGHT = "PI_BRIDGE_QUEUE_LIGHT_SLOTS"
# A single pass never launches more than this, whatever the slot budget says
# (a misconfigured budget of 500 must not fork 500 pi processes in one call).
MAX_START_PER_PASS = 8


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return default


def _as_slots(value, default: int) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return n if n >= 0 else default


def load_config() -> dict:
    """Merged queue config: defaults <- config.json "queue" <- env."""
    from . import bridge  # late import: bridge owns config.json access

    cfg = dict(DEFAULT_CONFIG)
    raw = bridge.bridge_config().get("queue")
    if isinstance(raw, dict):
        if "enabled" in raw:
            cfg["enabled"] = _as_bool(raw.get("enabled"), cfg["enabled"])
        if raw.get("heavy_slots") is not None:
            cfg["heavy_slots"] = _as_slots(raw.get("heavy_slots"),
                                           cfg["heavy_slots"])
        if raw.get("light_slots") is not None:
            cfg["light_slots"] = _as_slots(raw.get("light_slots"),
                                           cfg["light_slots"])
    env_enabled = (os.environ.get(ENV_ENABLED) or "").strip()
    if env_enabled:
        cfg["enabled"] = _as_bool(env_enabled, cfg["enabled"])
    if (os.environ.get(ENV_HEAVY) or "").strip():
        cfg["heavy_slots"] = _as_slots(os.environ.get(ENV_HEAVY),
                                       cfg["heavy_slots"])
    if (os.environ.get(ENV_LIGHT) or "").strip():
        cfg["light_slots"] = _as_slots(os.environ.get(ENV_LIGHT),
                                       cfg["light_slots"])
    return cfg


def is_enabled() -> bool:
    return bool(load_config()["enabled"])


# --------------------------------------------------------------------------
# job classification and queue state (pure functions over job dicts)
# --------------------------------------------------------------------------

def job_class(job: dict) -> str:
    """Job's slot class; unknown/absent values are heavy (back-compat)."""
    cls = job.get("job_class")
    return cls if cls in CLASSES else "heavy"


def new_block(job_id: str, cls: str) -> dict:
    """The `queue` block written at submit time (waiting, never started yet).

    `queued_at_ns` is the ordering key. `created_at`/`queued_at` are only
    second-resolution (the format every job.json has always used), so two
    jobs submitted inside the same second would otherwise be ordered by the
    random suffix of their ids -- i.e. not FIFO at all.
    """
    now = state.now_iso()
    return {"state": "waiting", "class": cls, "queued_at": now,
            "queued_at_ns": time.time_ns(), "started_at": None}


def normalize_block(job: dict) -> dict:
    """Read the queue block, synthesising it for pre-queue job.json files."""
    raw = job.get("queue")
    cls = job_class(job)
    if not isinstance(raw, dict):
        started = (job.get("last_run") or {}).get("started_at")
        return {"state": "running" if job.get("last_run") else "waiting",
                "class": cls,
                "queued_at": job.get("created_at"),
                "started_at": started}
    return {"state": raw.get("state") if raw.get("state") in
            ("waiting", "running") else ("running" if job.get("last_run")
                                         else "waiting"),
            "class": raw.get("class") if raw.get("class") in CLASSES else cls,
            "queued_at": raw.get("queued_at"),
            "queued_at_ns": raw.get("queued_at_ns"),
            "started_at": raw.get("started_at")}


def holds_slot(job: dict) -> bool:
    """True when this active job occupies a slot (running or claimed)."""
    if job.get("status") not in state.ACTIVE_STATUSES:
        return False
    return bool(job.get("last_run"))


def is_waiting(job: dict) -> bool:
    """True when the job waits for a slot: queued with no runner ever
    launched for the current turn (the anchor is still null)."""
    return (job.get("status") == "queued"
            and not job.get("last_run"))


def _order_key(job: dict) -> tuple:
    """Strict enqueue order: nanoseconds when known, else the second-resolution
    timestamp (legacy job.json files), with the job id as the last resort."""
    b = normalize_block(job)
    ns = b.get("queued_at_ns")
    if isinstance(ns, (int, float)) and ns > 0:
        key = int(ns)
    else:
        ts = _parse_iso_second(b.get("queued_at") or job.get("created_at"))
        key = int(ts * 1_000_000_000) if ts is not None else 0
    return (key, job.get("job_id") or "")


def _parse_iso_second(ts):
    if not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def waiting_jobs(jobs: list[dict]) -> list[dict]:
    return sorted([j for j in jobs if is_waiting(j)], key=_order_key)


def usage(jobs: list[dict]) -> dict:
    used = {c: 0 for c in CLASSES}
    for j in jobs:
        if holds_slot(j):
            used[job_class(j)] += 1
    return used


def position(job_id: str, jobs: list[dict] | None = None) -> int | None:
    """1-based FIFO position of a waiting job in its own class; None when the
    job is not waiting (or not found)."""
    if jobs is None:
        jobs = state.list_jobs()
    target = next((j for j in jobs if j.get("job_id") == job_id), None)
    if target is None or not is_waiting(target):
        return None
    cls = job_class(target)
    line = [j for j in waiting_jobs(jobs) if job_class(j) == cls]
    for i, j in enumerate(line, 1):
        if j.get("job_id") == job_id:
            return i
    return None


# --------------------------------------------------------------------------
# locking
# --------------------------------------------------------------------------

@contextlib.contextmanager
def _queue_lock_fd(fd):
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
        except OSError:
            pass


def acquire_queue_lock():
    """Non-reentrant bridge-wide lock around admission.  Returns an open fd
    on success, or None when another process holds it (the caller then simply
    does nothing: whoever holds the lock is doing the same pass)."""
    home = state.bridge_home()
    home.mkdir(parents=True, exist_ok=True)
    fd = open(home / "queue.lock", "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        fd.close()
        return None


# --------------------------------------------------------------------------
# admission
# --------------------------------------------------------------------------

def _claim(job_id: str) -> tuple[dict, int, dict] | None:
    """Reserve a slot for one waiting job (writer side of the queue).

    Returns (job, turn_number, anchor) on success, None when the job is no
    longer admissible (cancelled, started, or vanished).  The claim is
    visible to every other observer as soon as it is written: `last_run` is
    the same liveness anchor submit/feedback have always written, so
    reconcile, watchdog and origin delivery need no changes to see a claimed
    job as occupying a slot.
    """
    from . import bridge

    with state.job_lock(job_id):
        job = state.load_job(job_id)
        if job["status"] != "queued" or job.get("last_run"):
            return None
        if job.get("cancel_requested"):
            return None
        turns = job.get("turns") or []
        if not turns or turns[-1].get("finished_at"):
            # nothing to run: the open turn is already closed (a terminal
            # finalization raced us); nothing to admit.
            return None
        turn = turns[-1]["n"]
        launcher_kind = bridge._plan_launcher()
        anchor = bridge._pre_launch_anchor(job_id, turn, launcher_kind)
        job["last_run"] = anchor
        block = normalize_block(job)
        block["state"] = "running"
        block["class"] = job_class(job)
        block["started_at"] = state.now_iso()
        job["queue"] = block
        state.save_job(job)
        return job, turn, anchor


def _release_claim(job_id: str) -> None:
    """Undo a claim whose runner failed to start: back to waiting."""
    with state.job_lock(job_id):
        job = state.load_job(job_id)
        if job["status"] != "queued" or not job.get("last_run"):
            return
        job["last_run"] = None
        job["launcher_error"] = None
        state.save_job(job)


def admit(reason: str = "") -> dict:
    """One FIFO admission pass: start as many waiting jobs as the free slots
    allow.  Never touches a job that is not waiting.

    Raises BridgeError only for a launch failure of a job this pass claimed
    (the job is rolled back to waiting first, so a later pump retries it).
    """
    from . import bridge

    fd = acquire_queue_lock()
    if fd is None:
        return {"started": [], "skipped": "another-pass-running"}
    try:
        cfg = load_config()
        if not cfg["enabled"]:
            return {"started": [], "skipped": "queue-disabled"}
        jobs = state.list_jobs()
        used = usage(jobs)
        slots = {"heavy": cfg["heavy_slots"], "light": cfg["light_slots"]}
        started: list[dict] = []
        error: BridgeError | None = None
        for job in waiting_jobs(jobs):
            if len(started) >= MAX_START_PER_PASS:
                break
            cls = job_class(job)
            if used[cls] >= slots[cls]:
                continue
            claimed = _claim(job["job_id"])
            if claimed is None:
                continue
            job, turn, anchor = claimed
            used[cls] += 1
            try:
                launched = bridge._launch_runner(job["job_id"], turn, anchor)
                bridge._post_launch_merge(job["job_id"], launched)
            except BridgeError as e:
                _release_claim(job["job_id"])
                used[cls] -= 1
                if error is None:
                    error = e
                continue
            started.append({"job_id": job["job_id"], "class": cls,
                            "turn": turn})
        if error is not None:
            raise error
        return {"started": started, "usage": used, "slots": slots,
                "waiting": {c: len([j for j in waiting_jobs(state.list_jobs())
                                    if job_class(j) == c]) for c in CLASSES},
                "reason": reason or None}
    finally:
        _release_queue_lock(fd)


def _release_queue_lock(fd) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    except OSError:
        pass


def pump(reason: str = "") -> dict:
    """Admission pass that never raises (for pumps that must not break:
    the runner's own finalization, the watchdog timer, cancel)."""
    try:
        return admit(reason=reason)
    except Exception as e:  # a pump must never take its caller down
        return {"started": [], "error": f"{type(e).__name__}: {e}"[:300],
                "reason": reason or None}


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------

def view(job: dict, jobs: list[dict] | None = None) -> dict:
    """Per-job queue view (additive in pi_status; never affects semantics)."""
    b = normalize_block(job)
    if job.get("status") in state.TERMINAL_STATUSES:
        b["state"] = "finished"
        b["position"] = None
        b.pop("queued_at_ns", None)
        return b
    b["position"] = (position(job["job_id"], jobs)
                     if is_waiting(job) else None)
    b.pop("queued_at_ns", None)
    return b


def summary(jobs: list[dict] | None = None) -> dict:
    """Bridge-wide queue picture for `pi-bridge queue status`."""
    if jobs is None:
        jobs = state.list_jobs()
    cfg = load_config()
    used = usage(jobs)
    slots = {"heavy": cfg["heavy_slots"], "light": cfg["light_slots"]}
    waiting = waiting_jobs(jobs)
    return {
        "enabled": cfg["enabled"],
        "slots": slots,
        "running": used,
        "waiting": {c: len([j for j in waiting if job_class(j) == c])
                    for c in CLASSES},
        "queue": [{"job_id": j["job_id"], "class": job_class(j),
                   "queued_at": normalize_block(j)["queued_at"],
                   "task_preview": (j.get("task") or "")[:120]}
                  for j in waiting],
        "running_jobs": [{"job_id": j["job_id"], "class": job_class(j),
                          "started_at": normalize_block(j)["started_at"],
                          "status": j["status"]}
                         for j in jobs if holds_slot(j)],
    }
