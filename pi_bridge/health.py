"""Read-only view of the poller health records (`pi-bridge health`).

The bridge's Hermes plugin (plugin/__init__.py) writes one durable JSON
record per poller process at <bridge_home>/health/poller-<pid>.json (0600,
atomic tmp+rename, dir 0700): role, gate state, tick/scan timestamps,
pending count, last lifecycle events and counters.  Records are pure
bookkeeping -- they never contain task text, result text, ui_session_id or
secrets.

This module only READS that directory (stdlib only, no imports from the
plugin): live records are parsed, dead-pid records are skipped, corrupt
files are silently skipped, and files older than MAX_RECORD_AGE_S are
reaped best-effort so a restarted-under-new-pids deployment cannot pile up
records forever.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

RECORD_PREFIX = "poller-"
RECORD_SUFFIX = ".json"
RECORD_GLOB = RECORD_PREFIX + "*" + RECORD_SUFFIX
MAX_RECORD_AGE_S = 24 * 3600.0   # stale-by-age cleanup window


def health_dir(bridge_home) -> Path:
    return Path(bridge_home).expanduser().absolute() / "health"


def _pid_of(path: Path) -> int | None:
    stem = path.name
    if stem.startswith(RECORD_PREFIX) and stem.endswith(RECORD_SUFFIX):
        try:
            pid = int(stem[len(RECORD_PREFIX):-len(RECORD_SUFFIX)])
        except ValueError:
            return None
        return pid if pid > 0 else None
    return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True            # exists, owned by someone else
    except (OSError, ValueError):
        return False
    return True


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def read_records(bridge_home) -> list[dict]:
    """Parsed health records of LIVE poller processes, sorted by pid.

    Corrupt or unparsable files are skipped silently; files whose record is
    older than MAX_RECORD_AGE_S (by mtime) are removed best-effort; records
    of dead pids are hidden (a poller rewrites its own file with its pid, so
    nothing legitimate references them).
    """
    d = health_dir(bridge_home)
    try:
        files = sorted(d.glob(RECORD_GLOB))
    except OSError:
        return []
    out: list[dict] = []
    now = time.time()
    for p in files:
        pid = _pid_of(p)
        try:
            stale = (now - p.stat().st_mtime) > MAX_RECORD_AGE_S
        except OSError:
            continue
        if stale:                       # best-effort reap, pid or not
            _unlink_quietly(p)
            continue
        if pid is None:
            continue                    # not a pid-shaped name: skip
        if not _alive(pid):
            continue
        try:
            rec = json.loads(p.read_text("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue                          # corrupt: skip, never raise
        if not isinstance(rec, dict):
            continue
        rec = dict(rec)
        rec["pid"] = pid                      # file name is authoritative
        out.append(rec)
    return out
