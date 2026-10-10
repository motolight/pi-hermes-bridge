"""Origin delivery: hand the finished result back to whoever asked for it.

Why this exists (the 2026-10-07 incident)
-----------------------------------------
Until V1.4 the *user-visible* delivery of a finished job was entirely a
prompt instruction inside the wake run: the woken Hermes session was told to
run `hermes send -t <platform>:<chat_id>` or
`hermes --resume <ui_session_id> chat -q ... -Q` with its own terminal tool.
That path failed silently, three times over, and nothing reached the user:

1. the wake run executes in a **webhook** session, which has no channel a
   human can answer on -- when the dangerous-command approval gate fires
   there, the prompt goes into the route's log-only sink and the tool call
   simply blocks until `approvals.timeout` (60 s here) and is then denied
   fail-closed (`tools/approval.py:745-747`, "Silence is not consent");
2. the delivery command itself trips that gate as often as not, because the
   gate regex-matches the WHOLE command line, and the one-shot
   `chat -q "<free-form outcome text>"` interpolates untrusted result text
   into it (an outcome mentioning "systemctl restart x" is detected as
   "stop/restart system service");
3. after the third 60-second denial the model gave up, and its final reply
   went to the route's `deliver: log` sink -- so the job's `wake.delivered`
   was `true` (the wake POST was accepted) while the user saw nothing.

So delivery must not depend on a model's shell command.  This module runs
the Hermes CLI **directly as a subprocess, argv-only, never through a shell**
(outside the agent's terminal tool, so the approval gate is not involved at
all and quoted result text cannot be re-interpreted as a command), STRICTLY
by the job's recorded `origin`, with a hard timeout and no retries.

Policy (fixed, no configuration of routes)
------------------------------------------
* `origin.platform` is a messaging platform -> `hermes send -t
  <platform>:<chat_id>[:<thread_id>] <text> -q`
* `origin.platform == "webui"`           -> `hermes --resume <ui_session_id>
  chat -q <text> -Q --source tool` (best-effort; a leased/unknown session is
  a permanent failure, reported and never retried)
* origin empty, `local`, or an unknown platform -> deliver NOTHING anywhere.
  There is deliberately **no default channel and no Telegram fallback**.

Configuration (same file as the wake notifier; `~/.local/state/pi-bridge/wake.json`)
-------------------------------------------------------------------------------------

    {"enabled": true, "url": "...", "secret": "...",
     "origin_delivery": true,          # false -> this module is a no-op
     "hermes_bin": "",                 # optional absolute path
     "delivery_timeout": 240,          # seconds, SIGTERM then SIGKILL
     "delivery_max_chars": 1200}       # size of the delivered summary

`origin_delivery` defaults to **true**, and it is **independent of the wake
notifier**: `wake.json` `{"enabled": false}` silences the wake POST but not the
delivery.  `"origin_delivery": false` is the kill switch for delivery (V1.3
log-only behaviour).  The Hermes binary is discovered as `PI_BRIDGE_HERMES_BIN` >
`hermes_bin` > `PATH`, so tests (and odd installs) can point it at a stub.
The text is a plain summary; `MEDIA:` line prefixes and `[[as_document]]`
are neutralised so a result can never turn into a file attachment.

Robustness contract (identical to wake.py)
------------------------------------------
This module MUST NEVER change a job's status and MUST NEVER raise into the
runner.  Every outcome -- including "nothing to do" -- is recorded in the
job's `delivery` field and in runner.log.  A delivery failure is data, not
an error: `pi_status` remains the durable source of truth either way.
"""
from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import time

from . import state, wake

# Platforms whose chats a human reads asynchronously; `hermes send` reaches
# them by bot token.  Anything else is not a delivery target.
MESSAGING_PLATFORMS = frozenset({
    "telegram", "discord", "slack", "signal", "whatsapp",
    "mattermost", "matrix",
})
WEBUI_PLATFORM = "webui"

# A webui delivery is a real agent turn in the user's session: startup plus
# one (often slow, often large-history) model call.  90 s was measured too
# tight on a 46k-token session behind a local model -- the message landed but
# the resumed answer was cut off -- so the default is generous and the runner
# unit (TimeoutStartSec=infinity) is expected to stay alive that long.
DEFAULT_TIMEOUT = 240.0
DEFAULT_MAX_CHARS = 1200
MAX_TIMEOUT = 900.0
# SIGTERM grace before SIGKILL, so the CLI can close its session cleanly.
TERM_GRACE = 10.0
MIN_TIMEOUT = 1.0
OUTPUT_KEEP = 300
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

# Reason codes recorded in job["delivery"]["reason"] (never a secret).
REASON_OK = "ok"


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def load_config() -> dict:
    """Delivery knobs from wake.json.  Missing/broken -> safe defaults.

    Shares wake.json with the notifier on purpose: one operator knob per
    feature, and the secret is read by wake.load_config() alone (never
    re-exposed here).
    """
    raw = {}
    try:
        with open(wake.config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            raw = data
    except (FileNotFoundError, OSError, json.JSONDecodeError,
            UnicodeDecodeError):
        raw = {}
    enabled = raw.get("origin_delivery")
    out = {
        "enabled": True if enabled is None else bool(enabled),
        "hermes_bin": str(raw.get("hermes_bin") or "").strip(),
        "timeout": _env_float("PI_BRIDGE_DELIVERY_TIMEOUT",
                              raw.get("delivery_timeout")
                              if isinstance(raw.get("delivery_timeout"),
                                            (int, float)) else
                              DEFAULT_TIMEOUT),
        "max_chars": _env_float("PI_BRIDGE_DELIVERY_MAX_CHARS",
                                raw.get("delivery_max_chars")
                                if isinstance(raw.get("delivery_max_chars"),
                                              (int, float)) else
                                DEFAULT_MAX_CHARS),
    }
    out["timeout"] = min(MAX_TIMEOUT, max(MIN_TIMEOUT, out["timeout"]))
    out["max_chars"] = max(200, int(out["max_chars"]))
    return out


def discover_hermes_bin(cfg: dict) -> str:
    """PI_BRIDGE_HERMES_BIN > wake.json hermes_bin > PATH ('' if none)."""
    env = (os.environ.get("PI_BRIDGE_HERMES_BIN") or "").strip()
    if env:
        return env
    if cfg.get("hermes_bin"):
        return str(cfg["hermes_bin"])
    return shutil.which("hermes") or ""


# ---------------------------------------------------------------------------
# message text (plain text, never an attachment, never a command)
# ---------------------------------------------------------------------------

def _one_line(text: str, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def sanitize_text(text: str) -> str:
    """Neutralise the two markers Hermes' send path interprets.

    `MEDIA:<path>` becomes a native attachment (and is stripped from the
    body) and `[[as_document]]` forces document delivery; both are meaningful
    *content* inside a Pi result, so they are defanged rather than dropped.
    """
    text = re.sub(r"(?m)^MEDIA:", "[MEDIA:]", text)
    return text.replace("[[as_document]]", "")


def build_text(job: dict, max_chars: int) -> str:
    """The delivered summary: a fixed header, then the result (or error).

    Starts with a stable, non-flag-looking prefix so the text can never be
    parsed as an option by the CLI it is passed to.
    """
    status = job.get("status") or "finished"
    label = "выполнена" if status == "completed" else "завершилась ошибкой"
    header = (f"Pi-задача {job.get('job_id')} (turn "
              f"{len(job.get('turns') or [])}) {label}: "
              f"{_one_line(job.get('task') or '', 120)}")
    body = job.get("final_result") if status == "completed" else None
    if not body:
        body = job.get("error") or ""
    body = sanitize_text(str(body).strip())
    budget = max(200, max_chars - len(header) - 2)
    if len(body) > budget:
        body = body[:budget] + "…"
    text = f"{header}\n\n{body}" if body else header
    return text[:max_chars]


# ---------------------------------------------------------------------------
# target planning (pure: no subprocess, testable directly)
# ---------------------------------------------------------------------------

def plan(job: dict, hermes_bin: str, text: str) -> dict:
    """What delivery would look like for this job: {kind, channel, argv, reason}.

    `kind` is None for "deliver nowhere" (that is a *success* per policy, not
    a failure: there simply is no user channel).
    """
    if not hermes_bin:
        return {"kind": None, "channel": None, "argv": None,
                "reason": "hermes-binary-not-found"}
    if not os.path.isfile(hermes_bin):
        # An explicit PI_BRIDGE_HERMES_BIN / hermes_bin that points nowhere is
        # a misconfiguration, and it must read as one rather than as an
        # OSError from subprocess.
        return {"kind": None, "channel": None, "argv": None,
                "reason": "hermes-binary-not-found"}
    origin = job.get("origin") if isinstance(job.get("origin"), dict) else {}
    platform = origin.get("platform") or ""
    if platform == WEBUI_PLATFORM:
        sid = origin.get("ui_session_id") or origin.get("chat_id") or ""
        if not sid or not ID_RE.fullmatch(sid):
            return {"kind": None, "channel": None, "argv": None,
                    "reason": "no-webui-session-id"}
        return {
            "kind": "webui-resume",
            "channel": f"webui:{sid}",
            # argv-only, no shell: the outcome text is one opaque argument
            # and can never be re-read as a command line.
            "argv": [hermes_bin, "--resume", sid, "chat", "-q", text, "-Q",
                     "--source", "tool"],
            "reason": None,
        }
    if platform in MESSAGING_PLATFORMS:
        chat = origin.get("chat_id") or ""
        if not chat or not ID_RE.fullmatch(chat):
            return {"kind": None, "channel": None, "argv": None,
                    "reason": "no-chat-id"}
        target = f"{platform}:{chat}"
        thread = origin.get("thread_id") or ""
        if thread and ID_RE.fullmatch(thread):
            target += f":{thread}"
        return {
            "kind": "send",
            "channel": target,
            "argv": [hermes_bin, "send", "-t", target, text, "-q"],
            "reason": None,
        }
    return {"kind": None, "channel": None, "argv": None,
            "reason": "no-delivery-channel" if platform else "no-origin"}


# ---------------------------------------------------------------------------
# job-state bookkeeping
# ---------------------------------------------------------------------------

FIELDS = ("attempted", "ok", "channel", "kind", "reason", "error", "turn",
          "at")


def blank_state() -> dict:
    return {"attempted": False, "ok": False, "channel": None, "kind": None,
            "reason": None, "error": None, "turn": None, "at": None}


def normalize_state(job: dict) -> dict:
    d = job.get("delivery") if isinstance(job.get("delivery"), dict) else {}
    out = blank_state()
    for k in FIELDS:
        if k in d:
            out[k] = d[k]
    out["attempted"] = bool(out["attempted"])
    out["ok"] = bool(out["ok"])
    if isinstance(out["error"], str):
        out["error"] = out["error"][:300]
    return out


def _update(job_id: str, **fields) -> None:
    """Merge delivery fields into job.json under the lock (best effort)."""
    try:
        with state.job_lock(job_id):
            job = state.load_job(job_id)
            d = normalize_state(job)
            d.update(fields)
            job["delivery"] = d
            state.save_job(job)
    except Exception:
        pass


def _log(jd, msg: str) -> None:
    try:
        with open(jd / "runner.log", "a", encoding="utf-8") as f:
            f.write(f"{state.now_iso()} pid={os.getpid()} {msg}\n")
    except OSError:
        pass


class _DeliveryTimeout(Exception):
    """The delivery CLI outran its hard budget (already terminated)."""


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the delivery CLI's whole process group.

    The hermes CLI may hand its pipes to a grandchild (client, gateway
    connection); signalling only the direct child would then leave the pipes
    open and `communicate()` blocked *after* the deadline -- precisely the
    hang this budget exists to prevent.
    """
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass


def _run_bounded(argv: list, timeout: float) -> tuple:
    """Run argv to completion, or terminate its process group at the deadline.

    Never a shell, never a queue: argv only, output captured, own process
    group, SIGTERM before SIGKILL, and a bounded drain after the kill so a
    lingering descendant can never pin the runner past its budget.
    """
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        _signal_group(proc, signal.SIGTERM)
        try:
            proc.communicate(timeout=TERM_GRACE)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
            try:  # last bounded read; anything left is not worth waiting for
                proc.communicate(timeout=TERM_GRACE)
            except subprocess.TimeoutExpired:
                pass
        raise _DeliveryTimeout()


def _classify_returncode(stderr: str) -> str:
    """Map the CLI's typed refusal line to a reason code (no retry either way)."""
    m = re.search(r"hermes-refusal-reason:\s*([A-Z_]{1,64})", stderr or "")
    if m:
        return m.group(1).lower()
    if "Session not found" in (stderr or ""):
        return "session-not-found"
    return "cli-failed"


# ---------------------------------------------------------------------------
# entry point (runner calls this right after a terminal turn was persisted)
# ---------------------------------------------------------------------------

def deliver_result(job_id: str, turn_n: int) -> dict:
    """Deliver turn `turn_n` of `job_id` to its origin.  Never raises.

    Returns a small dict for runner.log.  Blocks for at most the configured
    timeout: a delivery is a courtesy, never a queue.
    """
    try:
        return _deliver(job_id, turn_n)
    except Exception as e:  # absolute guarantee for the caller
        try:
            _update(job_id, attempted=True, ok=False,
                    reason="internal", error=f"{type(e).__name__}",
                    turn=turn_n, at=state.now_iso())
        except Exception:
            pass
        return {"ok": False, "reason": f"internal:{type(e).__name__}"}


def _deliver(job_id: str, turn_n: int) -> dict:
    jd = state.job_dir(job_id)
    cfg = load_config()
    if not cfg["enabled"]:
        return {"ok": False, "reason": "disabled"}

    job = state.load_job(job_id)
    if len(job.get("turns") or []) > turn_n or job.get("cancel_requested"):
        # A newer turn (or a cancel) owns the outcome now; a stale summary
        # in the user's chat is worse than no summary.
        return {"ok": False, "reason": "superseded"}
    if (job.get("delivery") or {}).get("turn") == turn_n and \
            (job.get("delivery") or {}).get("attempted"):
        return {"ok": False, "reason": "already-attempted"}

    text = build_text(job, cfg["max_chars"])
    hermes = discover_hermes_bin(cfg)
    p = plan(job, hermes, text)

    if p["kind"] is None:
        # Nothing to do is the normal outcome for local/unknown origins.
        reason = p["reason"]
        attempted = reason in ("hermes-binary-not-found",)
        _update(job_id, attempted=attempted, ok=False, channel=None,
                kind=None, reason=reason, error=None, turn=turn_n,
                at=state.now_iso())
        _log(jd, f"turn {turn_n} delivery: nothing delivered ({reason})")
        return {"ok": False, "reason": reason}

    started = time.monotonic()
    _log(jd, f"turn {turn_n} delivery -> {p['channel']} ({p['kind']}, "
             f"{len(text)} chars)")
    try:
        rc, out, err = _run_bounded(p["argv"], cfg["timeout"])
    except _DeliveryTimeout:
        dur = time.monotonic() - started
        _update(job_id, attempted=True, ok=False, channel=p["channel"],
                kind=p["kind"], reason="timeout",
                error=f"killed after {cfg['timeout']:.0f}s", turn=turn_n,
                at=state.now_iso())
        _log(jd, f"turn {turn_n} delivery -> {p['channel']}: timed out after "
                 f"{dur:.1f}s (no retry)")
        return {"ok": False, "reason": "timeout", "channel": p["channel"]}
    except OSError as e:
        _update(job_id, attempted=True, ok=False, channel=p["channel"],
                kind=p["kind"], reason="not-runnable",
                error=str(e)[:300], turn=turn_n, at=state.now_iso())
        _log(jd, f"turn {turn_n} delivery -> {p['channel']}: not runnable: {e}")
        return {"ok": False, "reason": "not-runnable",
                "channel": p["channel"]}

    if rc == 0:
        _update(job_id, attempted=True, ok=True, channel=p["channel"],
                kind=p["kind"], reason=REASON_OK, error=None, turn=turn_n,
                at=state.now_iso())
        _log(jd, f"turn {turn_n} delivery -> {p['channel']}: ok")
        return {"ok": True, "channel": p["channel"], "kind": p["kind"]}

    reason = _classify_returncode(err)
    _update(job_id, attempted=True, ok=False, channel=p["channel"],
            kind=p["kind"], reason=reason,
            error=(err.strip() or out.strip() or f"rc={rc}")[:OUTPUT_KEEP],
            turn=turn_n, at=state.now_iso())
    _log(jd, f"turn {turn_n} delivery -> {p['channel']}: {reason} (rc={rc}); "
             "no retry, no fallback channel")
    return {"ok": False, "reason": reason, "channel": p["channel"], "rc": rc}
