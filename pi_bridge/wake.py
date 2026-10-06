"""Wake notifier: tell Hermes, without a human, that a Pi turn is finished.

Why this exists
---------------
After `pi_delegate` the Hermes foreground turn may end long before the Pi
job finishes.  The wake notifier is the callback that closes that loop: when
a turn reaches a TERMINAL status (completed / failed -- never cancelled),
the runner POSTs a signed summary to the Hermes gateway webhook platform,
which starts an autonomous Hermes run.  That run checks the result with
pi_status, either reports the outcome to the user or sends pi_feedback into
the SAME Pi session (whose next turn wakes Hermes again).

Configuration (absent == feature off, behaviour identical to V1)
----------------------------------------------------------------
``~/.local/state/pi-bridge/wake.json`` (i.e. ``$PI_BRIDGE_HOME/wake.json``)::

    {"enabled": true, "url": "http://127.0.0.1:8644/webhooks/pi-bridge-complete",
     "secret": "***"}

The secret is never logged, never stored in job state and never returned by
status/CLI.

Signature scheme (verified against
``~/.hermes/hermes-agent/gateway/platforms/webhook.py``)
--------------------------------------------------------------------------------
Generic HMAC "V2", which is the scheme that scheme-dispatch reaches for a
self-built client (webhook.py:649-659):

    X-Webhook-Timestamp:    <integer unix seconds>   (freshness +-300 s)
    X-Webhook-Signature-V2: hex( HMAC-SHA256(secret, "<timestamp>." + raw_body) )

The signature must cover the EXACT bytes sent, so a retry re-signs with a
fresh timestamp (a 45-minute retry loop would otherwise be rejected as stale
after 5 minutes).  ``X-Request-ID`` carries a delivery id that is CONSTANT
across retries of the same turn, so the gateway's 1-hour idempotency cache
turns duplicate deliveries into ``200 {"status":"duplicate"}`` instead of a
second agent run (webhook.py:552-557).

Delivery policy
---------------
Retry every ``PI_BRIDGE_WAKE_INTERVAL`` seconds (default 30) up to
``PI_BRIDGE_WAKE_MAX_ATTEMPTS`` (default 90, i.e. ~45 min) until HTTP 2xx --
this survives a gateway restart: while the webhook platform is down the
runner keeps posting until it comes back.  Errors that cannot be fixed by
waiting (401/403/404/413 -- wrong secret, unknown/disabled route) are
abandoned after ``PI_BRIDGE_WAKE_PERMANENT_AFTER`` attempts so a
misconfiguration does not pin the runner for 45 minutes.  SIGTERM/SIGINT
(``systemctl --user stop <unit>``) breaks the loop immediately -- including
mid-sleep -- and posts nothing further.

Robustness contract: this module MUST NEVER change a job's status and MUST
NEVER raise into the runner.  Every failure is recorded in the job's ``wake``
field (enabled / delivered / attempts / last_error) and in runner.log.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import signal
import ssl
import threading
import time
from http.client import HTTPException, HTTPConnection, HTTPSConnection
from pathlib import Path
from urllib.parse import urlsplit

from . import piweb, state

EVENT = "pi_bridge_turn_complete"

# Only these terminal statuses wake Hermes; a cancelled job was cancelled by
# a human, who already knows.
WAKE_STATUSES = ("completed", "failed")

RESULT_EXCERPT = 1500
TASK_PREVIEW = 200
ERROR_EXCERPT = 800
URL_KEEP = 300

DEFAULT_INTERVAL = 30.0
DEFAULT_MAX_ATTEMPTS = 90
DEFAULT_HTTP_TIMEOUT = 15.0
DEFAULT_PIWEB_BUDGET = 3.0
# Give up early on answers that more waiting cannot fix.
DEFAULT_PERMANENT_AFTER = 3
PERMANENT_HTTP_STATUSES = (400, 401, 403, 404, 413)

# Never let a hostile/oversized config stall the runner.
CONNECT_TIMEOUT_MAX = 30.0

# Set by SIGTERM/SIGINT while a wake is in flight (see request_stop).
_stop = threading.Event()


def request_stop() -> None:
    """Abandon an in-flight retry loop as soon as possible.

    `notify()` installs a handler for this, so `systemctl --user stop <unit>`
    really stops the runner instead of burning TimeoutStopSec and being
    SIGKILLed -- and stops POSTing, which matters when the operator stops the
    unit precisely because they are taking the webhook route away.
    """
    _stop.set()


def _stop_signal(signum, frame):
    _stop.set()


def _install_stop_handlers():
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, _stop_signal)
        except (OSError, ValueError):  # not the main thread, no handler here
            continue
    return previous


def _restore_handlers(previous) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError, TypeError):
            continue


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def config_path() -> Path:
    return state.bridge_home() / "wake.json"


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, "") or default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(float(os.environ.get(name, "") or default)))
    except ValueError:
        return default


def load_config() -> dict:
    """Read wake.json.  Missing / unreadable / malformed / disabled -> off.

    Never raises and never echoes the secret.
    """
    out = {"enabled": False, "url": "", "secret": ""}
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return out
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return out
    if not isinstance(raw, dict):
        return out
    if raw.get("enabled") is not True:
        return out
    url = raw.get("url")
    secret = raw.get("secret")
    if not isinstance(url, str) or not url.strip():
        return out
    if not isinstance(secret, str) or not secret.strip():
        return out
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        return out
    out.update({"enabled": True, "url": url[:URL_KEEP], "secret": secret})
    return out


def is_enabled() -> bool:
    return load_config()["enabled"]


# ---------------------------------------------------------------------------
# payload + signing
# ---------------------------------------------------------------------------

def _clip(text, limit: int) -> str | None:
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    return text[:limit]


def _pi_web_url(job: dict) -> str | None:
    """Best-effort PI WEB deep link for the wake payload (never fatal)."""
    if (os.environ.get("PI_BRIDGE_NO_PIWEB", "") or "").lower() in (
            "1", "true", "yes", "on"):
        return None
    try:
        view = piweb.observe_job(job, budget_seconds=_env_float(
            "PI_BRIDGE_WAKE_PIWEB_BUDGET", DEFAULT_PIWEB_BUDGET))
    except Exception:  # observability must never affect the wake path
        return None
    url = view.get("pi_web_url") if isinstance(view, dict) else None
    return url[:URL_KEEP] if isinstance(url, str) and url else None


def build_payload(job: dict, turn_n: int) -> dict:
    """The JSON body of the wake call.  Summary only -- no transcript, no
    stderr, no secrets."""
    payload = {
        "event": EVENT,
        # Same value under the name the gateway's route `events` filter
        # actually reads (webhook.py:525-526 resolves event_type from the
        # body), so a subscription can be scoped to this event only.
        "event_type": EVENT,
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "turn": turn_n,
        "cwd": job.get("cwd"),
        "task_preview": _clip(job.get("task"), TASK_PREVIEW),
        "final_result_excerpt": _clip(job.get("final_result"), RESULT_EXCERPT),
        "pi_session_id": job.get("pi_session_id"),
        "completed_at": state.now_iso(),
    }
    err = _clip(job.get("error"), ERROR_EXCERPT)
    if err:
        payload["error"] = err
    url = _pi_web_url(job)
    if url:
        payload["pi_web_url"] = url
    return payload


def delivery_id(job_id: str, turn_n: int) -> str:
    """Stable across retries of one turn -> the gateway dedups duplicates."""
    return f"{job_id}-t{turn_n}"


def sign(secret: str, body: bytes, timestamp: str) -> str:
    """Hermes webhook generic V2: hex HMAC-SHA256 over "<timestamp>.<body>"."""
    return hmac.new(secret.encode("utf-8"),
                    timestamp.encode("ascii") + b"." + body,
                    hashlib.sha256).hexdigest()


def render_request(cfg: dict, payload: dict, job_id: str,
                   turn_n: int) -> tuple[bytes, dict]:
    """(body_bytes, headers) for one attempt.  Re-called on every retry so
    the timestamp -- and therefore the signature -- is always fresh."""
    body = json.dumps(payload, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    ts = str(int(time.time()))
    headers = {
        # Exact header names as the gateway reads them (webhook.py:649-651).
        # urllib would rewrite their case, so the client below is
        # http.client and signs/sends these bytes verbatim.
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Webhook-Timestamp": ts,
        "X-Webhook-Signature-V2": sign(cfg["secret"], body, ts),
        # Constant for the whole retry sequence of this turn: a duplicate
        # delivery answers 200 {"status":"duplicate"} instead of starting a
        # second agent run.
        "X-Request-ID": delivery_id(job_id, turn_n),
    }
    return body, headers


# ---------------------------------------------------------------------------
# one attempt (never raises)
# ---------------------------------------------------------------------------

def _connect(url: str, timeout: float):
    parts = urlsplit(url)
    if parts.scheme == "http":
        host = parts.hostname or ""
        conn = HTTPConnection(host, parts.port or 80, timeout=timeout)
    elif parts.scheme == "https":
        host = parts.hostname or ""
        conn = HTTPSConnection(host, parts.port or 443, timeout=timeout,
                              context=ssl.create_default_context())
    else:
        raise ValueError(f"unsupported wake url scheme: {parts.scheme!r}")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return conn, target


def post_once(cfg: dict, payload: dict, job_id: str,
              turn_n: int) -> tuple[bool, int | None, str | None]:
    """One signed POST. Returns (ok, http_status, error).

    A fresh connection per attempt (a restarted gateway must be reachable
    again immediately), no proxies, no redirects, exact header case.
    """
    body, headers = render_request(cfg, payload, job_id, turn_n)
    timeout = min(CONNECT_TIMEOUT_MAX, _env_float(
        "PI_BRIDGE_WAKE_HTTP_TIMEOUT", DEFAULT_HTTP_TIMEOUT))
    conn = None
    status = None

    def _close():
        nonlocal conn
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            conn = None

    try:
        conn, target = _connect(cfg["url"], timeout)
        conn.request("POST", target, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp.read(4096)  # drain so the connection can close cleanly
        _close()
    except HTTPException as e:
        _close()
        return False, None, f"bad response: {type(e).__name__}"[:200]
    except TimeoutError:
        _close()
        return False, None, "timeout"
    except (OSError, ValueError) as e:
        _close()
        detail = str(e) or type(e).__name__
        return False, None, f"unreachable: {detail}"[:200]
    except Exception as e:  # never let the notifier break a job
        _close()
        return False, None, f"{type(e).__name__}"[:200]
    if isinstance(status, int) and 200 <= status < 300:
        return True, status, None
    return False, status, f"HTTP {status}"


def _permanent(status: int | None) -> bool:
    return isinstance(status, int) and status in PERMANENT_HTTP_STATUSES


# ---------------------------------------------------------------------------
# job-state bookkeeping (4 keys, no secrets)
# ---------------------------------------------------------------------------

def blank_state(enabled: bool) -> dict:
    return {"enabled": bool(enabled), "delivered": False, "attempts": 0,
            "last_error": None}


def normalize_state(job: dict, enabled: bool | None = None) -> dict:
    """The four public wake fields.

    `enabled` is normally the stored value (written at submit/feedback time);
    pass it explicitly to report the LIVE configuration instead -- views do,
    so a wake.json that was added or removed after submit is reflected
    without touching job state.
    """
    w = job.get("wake") if isinstance(job.get("wake"), dict) else {}
    out = {"enabled": bool(w.get("enabled")),
           "delivered": bool(w.get("delivered")),
           "attempts": int(w.get("attempts") or 0),
           "last_error": (str(w["last_error"])[:300]
                          if w.get("last_error") else None)}
    if enabled is not None:
        out["enabled"] = bool(enabled)
    return out


def _update(job_id: str, **fields) -> dict | None:
    """Merge wake fields into job.json under the lock. Returns fresh job."""
    try:
        with state.job_lock(job_id):
            job = state.load_job(job_id)
            w = normalize_state(job)
            w.update(fields)
            job["wake"] = w
            state.save_job(job)
            return job
    except Exception:
        return None


def _log(jd: Path, msg: str) -> None:
    try:
        with open(jd / "runner.log", "a", encoding="utf-8") as f:
            f.write(f"{state.now_iso()} pid={os.getpid()} {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# entry point (called by the runner after a terminal turn was persisted)
# ---------------------------------------------------------------------------

def notify(job_id: str, turn_n: int) -> dict:
    """Wake Hermes about turn `turn_n` of `job_id`. Never raises.

    Blocks until the webhook accepted the delivery or the retry budget is
    spent: the runner unit stays alive on purpose so that a gateway restart
    is survived (the same process keeps trying).
    """
    try:
        return _notify(job_id, turn_n)
    except Exception as e:  # absolute guarantee for the caller
        try:
            _update(job_id, last_error=f"internal: {type(e).__name__}")
        except Exception:
            pass
        return {"delivered": False, "error": str(e)}


def _notify(job_id: str, turn_n: int) -> dict:
    _stop.clear()
    previous = _install_stop_handlers()
    try:
        return _deliver(job_id, turn_n)
    finally:
        _restore_handlers(previous)


def _deliver(job_id: str, turn_n: int) -> dict:
    jd = state.job_dir(job_id)
    cfg = load_config()
    if not cfg["enabled"]:
        # Feature off: no requests, no state churn, exactly V1 behaviour.
        return {"delivered": False, "reason": "disabled"}

    job = _update(job_id, enabled=True, delivered=False, attempts=0,
                  last_error=None)
    if job is None:  # job vanished
        return {"delivered": False, "reason": "job-missing"}
    payload = build_payload(job, turn_n)

    interval = _env_float("PI_BRIDGE_WAKE_INTERVAL", DEFAULT_INTERVAL)
    max_attempts = _env_int("PI_BRIDGE_WAKE_MAX_ATTEMPTS",
                            DEFAULT_MAX_ATTEMPTS)
    permanent_after = _env_int("PI_BRIDGE_WAKE_PERMANENT_AFTER",
                               DEFAULT_PERMANENT_AFTER)

    attempts = 0
    last_error = None

    def _stopped() -> dict:
        _update(job_id, attempts=attempts,
                last_error="abandoned: stopped by signal (SIGTERM/SIGINT)")
        _log(jd, f"wake t{turn_n}: stopped by signal after {attempts} "
                 "attempt(s); no further posts")
        return {"delivered": False, "attempts": attempts, "reason": "stopped"}

    for attempt in range(1, max_attempts + 1):
        if _stop.is_set():
            return _stopped()
        # Stop quietly when this wake became irrelevant: the job was
        # muted/cancelled, or a newer turn (pi_feedback) took over.
        try:
            fresh = state.load_job(job_id)
        except state.BridgeError:
            _log(jd, f"wake t{turn_n}: job gone, stop")
            return {"delivered": False, "reason": "job-missing"}
        if fresh.get("cancel_requested") or len(fresh.get("turns") or []) > turn_n:
            _log(jd, f"wake t{turn_n}: superseded by a newer turn or cancel, "
                     "stop without waking")
            _update(job_id, attempts=attempts,
                    last_error="superseded: newer turn or cancel requested")
            return {"delivered": False, "reason": "superseded"}

        attempts = attempt
        ok, status, err = post_once(cfg, payload, job_id, turn_n)
        if ok:
            _update(job_id, delivered=True, attempts=attempts, last_error=None)
            _log(jd, f"wake t{turn_n}: delivered (HTTP {status}, "
                     f"attempt {attempt})")
            return {"delivered": True, "attempts": attempts, "status": status}
        last_error = err or "unknown error"
        _update(job_id, delivered=False, attempts=attempts,
                last_error=last_error)
        _log(jd, f"wake t{turn_n}: attempt {attempt} failed: {last_error} "
                 f"(url host only, secret never logged)")
        if _permanent(status) and attempt >= permanent_after:
            _update(job_id, last_error=f"{last_error} (permanent, giving up)")
            return {"delivered": False, "attempts": attempts,
                    "reason": "permanent"}
        if attempt < max_attempts:
            if _stop.wait(interval):   # interruptible sleep
                return _stopped()

    _log(jd, f"wake t{turn_n}: giving up after {attempts} attempts "
             f"({last_error})")
    return {"delivered": False, "attempts": attempts,
            "reason": "attempts-exhausted"}
