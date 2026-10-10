"""Bridge runner: the durable worker process for one job turn.

Launched by `pi-bridge submit|feedback` either as a systemd-run --user
transient unit or as a detached process.  Runs pi in print mode with the
task delivered on stdin, then persists the terminal status.

Internal entry point: python -m pi_bridge.runner <job_id> --turn N --home DIR
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from . import deliver, state, wake
from .state import BridgeError

RESULT_KEEP = 8000            # chars of final result kept in job.json
OUT_READ_LIMIT = 10 * 1024 * 1024
ERR_READ_LIMIT = 16 * 1024


def _log(jd: Path, msg: str) -> None:
    try:
        with open(jd / "runner.log", "a", encoding="utf-8") as f:
            f.write(f"{state.now_iso()} pid={os.getpid()} {msg}\n")
    except OSError:
        pass


def standard_store_root() -> Path:
    """Pi's default session store -- the same one PI WEB reads.

    Layout: <root>/--<normalized-cwd>--/[subagents/]<ts>_<session-id>.jsonl
    """
    return Path.home() / ".pi" / "agent" / "sessions"


def _session_file_re(session_id: str) -> re.Pattern:
    """pi names session files ``<timestamp>_<session-id>.jsonl`` and its
    timestamp contains no underscore, so the prefix is matched exactly --
    a bare ``*_`` glob could suffix-match a *different* session whose id ends
    with ours (session ids may contain underscores)."""
    return re.compile(r"^[^_]+_" + re.escape(session_id) + r"\.jsonl$")


def find_session_file_in(root: Path, session_id: str) -> str | None:
    """Newest file under `root` that is exactly this session's file."""
    try:
        pat = _session_file_re(session_id)
    except re.error:
        return None
    matches = []
    for p in glob.glob(str(Path(root) / "**" / "*.jsonl"), recursive=True):
        if not pat.match(os.path.basename(p)):
            continue
        try:
            matches.append((os.path.getmtime(p), p))
        except OSError:
            continue
    return max(matches)[1] if matches else None


def find_session_file(sessions_dir: Path, session_id: str) -> str | None:
    """Locate the session file of `session_id`.

    Primary: Pi's standard store (~/.pi/agent/sessions), which is what the
    runner now lets pi write to and what PI WEB lists.  Fallback: the
    per-job sessions dir used by bridge versions <= V1 (those jobs keep
    working); its sessions are simply invisible to PI WEB.
    """
    return (find_session_file_in(standard_store_root(), session_id)
            or find_session_file_in(sessions_dir, session_id))


def run_turn(job_id: str, turn_n: int) -> int:
    jd = state.job_dir(job_id)
    with state.job_lock(job_id):
        job = state.load_job(job_id)
        if job["status"] in state.TERMINAL_STATUSES and job.get("cancel_requested"):
            _log(jd, "cancel requested before start; not running")
            return 0
        if turn_n - 1 >= len(job["turns"]) or job["turns"][turn_n - 1]["n"] != turn_n:
            _log(jd, f"turn {turn_n} not found in job state; aborting")
            return 1
        job["status"] = "running"
        job["last_run"] = dict(job.get("last_run") or {})
        job["last_run"].update({"pid": os.getpid(), "started_at": state.now_iso()})
        state.save_job(job)

    if turn_n == 1:
        task_text = job["task"]
    else:
        fb_idx = turn_n - 2
        if fb_idx >= len(job["feedbacks"]):
            _log(jd, f"feedback for turn {turn_n} missing; aborting")
            with state.job_lock(job_id):
                job = state.load_job(job_id)
                job["status"] = "failed"
                job["error"] = "internal: feedback text missing for turn"
                state.save_job(job)
            return 1
        task_text = job["feedbacks"][fb_idx]["text"]

    turns_dir = jd / "turns"
    turns_dir.mkdir(exist_ok=True)
    state.write_text_atomic(turns_dir / f"{turn_n:02d}.task.txt", task_text)
    out_path = turns_dir / f"{turn_n:02d}.out"
    err_path = turns_dir / f"{turn_n:02d}.err"

    pi_argv = [
        job["pi_bin"], "--print",
        "--agent", "orchestrator",
        # No --session-dir on purpose: pi writes the main session into its
        # standard store (~/.pi/agent/sessions/<safe-cwd>/), i.e. the store
        # PI WEB reads.  Same-session continuation via --session-id works
        # there too (verified).  The env-var session-dir override is
        # deliberately not used either.
        "--session-id", job["pi_session_id"],
    ]

    # Legacy jobs (created by bridge <= V1, session under jobs/<id>/sessions)
    # MUST keep their --session-dir: pi only resolves --session-id inside the
    # store it is pointed at, and on a miss it silently creates a NEW session
    # with the same id -- which would look like a continuation while losing
    # the whole context.  New jobs never have that dir populated, so they
    # always take the standard store (and become observable).
    legacy_session = find_session_file_in(jd / "sessions", job["pi_session_id"])
    standard_session = find_session_file_in(standard_store_root(),
                                            job["pi_session_id"])
    # A job that *has* a legacy session dir keeps using it, even if a
    # standard-store file with the same id exists: that file can only be the
    # contextless session pi re-created during the buggy window between the
    # first V1.1 commit and this one, so the job dir holds the real history.
    legacy_mode = legacy_session is not None
    if legacy_mode:
        pi_argv += ["--session-dir", str(jd / "sessions")]
        if standard_session:
            _log(jd, f"turn {turn_n}: both a legacy session ({legacy_session}) "
                     f"and a standard-store file ({standard_session}) exist for "
                     "this id; continuing the legacy one (real context)")
        else:
            _log(jd, f"turn {turn_n} continues a pre-V1.1 session in "
                     f"{legacy_session} (legacy --session-dir)")
    env = os.environ.copy()
    for k, v in (job.get("env_snapshot") or {}).items():
        if k in ("PATH", "HOME", "LANG", "LC_ALL"):
            env[k] = v

    _log(jd, f"turn {turn_n} starting pi session={job['pi_session_id']} cwd={job['cwd']}")

    cancelled = {"flag": False}

    def on_term(signum, frame):
        cancelled["flag"] = True
        _log(jd, f"turn {turn_n} received signal {signum}")
        try:
            child.terminate()
        except Exception:
            pass

    with open(out_path, "wb") as out_f, open(err_path, "wb") as err_f:
        child = subprocess.Popen(
            pi_argv,
            cwd=job["cwd"],
            stdin=subprocess.PIPE,
            stdout=out_f,
            stderr=err_f,
            env=env,
        )
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)
        # record pi pid + process identity under the lock (merge with any
        # concurrent cancel flag). pi_start survives real pi rewriting its
        # own cmdline (process.title), unlike cmdline-based checks.
        pi_start = state.pid_start_ticks(child.pid)
        with state.job_lock(job_id):
            fresh = state.load_job(job_id)
            if turn_n - 1 < len(fresh["turns"]):
                fresh["turns"][turn_n - 1]["pi_pid"] = child.pid
                fresh["turns"][turn_n - 1]["pi_start"] = pi_start
            state.save_job(fresh)
        job["turns"][turn_n - 1]["pi_pid"] = child.pid
        job["turns"][turn_n - 1]["pi_start"] = pi_start
        try:
            child.stdin.write(task_text.encode("utf-8"))
            child.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # pi already gone; exit code below will describe it
        exit_code = child.wait()

    _log(jd, f"turn {turn_n} pi exited rc={exit_code} cancelled={cancelled['flag']}")

    # Runner owns finalization; re-read under the lock to pick up any
    # cancel_requested flag set concurrently by the cancel command.
    full_final, out_capped = state.read_bounded_text(out_path, OUT_READ_LIMIT)
    full_final = full_final.strip()
    err_text, _ = state.read_bounded_text(err_path, ERR_READ_LIMIT)

    with state.job_lock(job_id):
        job = state.load_job(job_id)
        t = job["turns"][turn_n - 1] if turn_n - 1 < len(job["turns"]) else None
        if t is None or t.get("n") != turn_n:
            _log(jd, f"turn {turn_n} vanished from state; skipping finalize")
            return 0
        t["finished_at"] = state.now_iso()
        t["exit_code"] = exit_code
        if len(job["turns"]) != turn_n:
            # A newer turn was already opened (e.g. cancel -> immediate
            # feedback): record only this turn's fields.
            _log(jd, f"turn {turn_n} is no longer current; not writing "
                     "global status fields")
            state.save_job(job)
            return 0
        # Record the file pi actually wrote this turn, in the store used.
        used_root = (jd / "sessions") if legacy_mode else standard_store_root()
        job["pi_session_file"] = (find_session_file_in(used_root, job["pi_session_id"])
                                  or find_session_file(jd / "sessions",
                                                       job["pi_session_id"]))

        if cancelled["flag"] or job.get("cancel_requested"):
            job["status"] = "cancelled"
            job["error"] = None
        elif exit_code == 0:
            job["status"] = "completed"
            job["error"] = None
        else:
            job["status"] = "failed"
            tail = err_text.strip()
            job["error"] = (f"pi exited with code {exit_code}"
                            + (f"; stderr tail: {tail[-2000:]}" if tail else ""))

        if full_final:
            job["final_result"] = full_final[:RESULT_KEEP]
            job["final_result_chars"] = len(full_final)
            job["final_result_truncated"] = len(full_final) > RESULT_KEEP
            if out_capped:
                job["stdout_capped"] = True
                _log(jd, f"turn {turn_n}: pi stdout exceeded {OUT_READ_LIMIT} "
                         "decoded characters; final_result.md is a capped copy")
            state.write_text_atomic(jd / "final_result.md", full_final)
        else:
            job["final_result"] = None
            job["final_result_chars"] = 0
            job["final_result_truncated"] = False

        state.save_job(job)
    _log(jd, f"turn {turn_n} done status={job['status']}")

    # Hand the outcome back to whoever asked for it (completed/failed only).
    # Runs BEFORE the wake notifier on purpose: the user-visible result must
    # not wait on the webhook (which may be down and retrying for up to 45
    # minutes), and the wake run is a quality check, not a delivery channel.
    # Never changes the job status and never raises.
    if job["status"] in wake.WAKE_STATUSES:
        try:
            delivered = deliver.deliver_result(job_id, turn_n)
        except Exception as e:  # deliver_result guarantees this; belt and braces
            delivered = {"ok": False, "reason": f"internal:{type(e).__name__}"}
        _log(jd, f"turn {turn_n} origin delivery: {delivered}")

    # Wake Hermes (completed/failed only; a cancelled turn was cancelled by
    # a human).  Blocks until the webhook accepted the delivery or the retry
    # budget is spent, so the runner unit stays alive on purpose; it can
    # never change the job status and never raises.
    if job["status"] in wake.WAKE_STATUSES:
        outcome = wake.notify(job_id, turn_n)
        _log(jd, f"turn {turn_n} wake outcome: {outcome}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pi-bridge-runner", add_help=False)
    ap.add_argument("job_id")
    ap.add_argument("--turn", type=int, required=True)
    ap.add_argument("--home", required=True)
    args = ap.parse_args(argv)
    os.environ["PI_BRIDGE_HOME"] = args.home
    try:
        return run_turn(args.job_id, args.turn)
    except BridgeError as e:
        print(f"pi-bridge runner error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
