"""Persistent state for pi-bridge jobs.

State root: ~/.local/state/pi-bridge (override with PI_BRIDGE_HOME).
All writes are atomic (tmp file + os.replace).  No secrets are ever stored.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

JOB_ID_RE = re.compile(r"^pb-\d{8}T\d{6}Z-[0-9a-f]{6}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
UNIT_RE = re.compile(r"^pi-bridge-pb-\d{8}T\d{6}Z-[0-9a-f]{6}-t\d{1,4}$")

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
ACTIVE_STATUSES = {"queued", "running"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BridgeError(Exception):
    """User-facing structured error."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def bridge_home() -> Path:
    home = os.environ.get("PI_BRIDGE_HOME")
    if home:
        return Path(home).expanduser().absolute()
    return Path.home() / ".local" / "state" / "pi-bridge"


def jobs_root() -> Path:
    return bridge_home() / "jobs"


def job_dir(job_id: str) -> Path:
    if not JOB_ID_RE.match(job_id):
        raise BridgeError(f"invalid job id: {job_id!r}")
    return jobs_root() / job_id


def gen_job_id() -> str:
    import secrets

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"pb-{ts}-{secrets.token_hex(3)}"


@contextlib.contextmanager
def job_lock(job_id: str):
    """Exclusive per-job advisory lock (flock) for read-modify-write
    transitions of job.json.  Not re-entrant: never nest for the same job."""
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    f = open(d / ".lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def pid_start_ticks(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat (start time in clock ticks): a PID-reuse
    safe identity that survives processes rewriting their own cmdline."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode("ascii", "replace")
        rest = data[data.rindex(")") + 2:].split()
        return int(rest[19])
    except (OSError, ValueError, IndexError):
        return None


def save_job(job: dict) -> None:
    job = dict(job)
    job["updated_at"] = now_iso()
    d = job_dir(job["job_id"])
    d.mkdir(parents=True, exist_ok=True)
    target = d / "job.json"
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".job-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_job(job_id: str) -> dict:
    path = job_dir(job_id) / "job.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            job = json.load(f)
    except FileNotFoundError:
        raise BridgeError(f"unknown job id: {job_id}") from None
    except json.JSONDecodeError as e:
        raise BridgeError(f"corrupt job state for {job_id}: {e}", code=1) from None
    if job.get("job_id") != job_id:
        raise BridgeError(f"job state mismatch for {job_id}", code=1)
    return job


def list_jobs() -> list[dict]:
    root = jobs_root()
    if not root.is_dir():
        return []
    jobs = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and JOB_ID_RE.match(d.name):
            try:
                jobs.append(load_job(d.name))
            except BridgeError:
                continue
    return jobs


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_bounded_text(path: Path, limit: int) -> tuple[str, bool]:
    """Read at most `limit` chars from a text file. Returns (text, truncated)."""
    try:
        size = path.stat().st_size
    except OSError:
        return "", False
    if size == 0:
        return "", False
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read(limit + 1)
    if len(text) > limit:
        return text[:limit], True
    return text, False
