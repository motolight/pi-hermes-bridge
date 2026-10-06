"""pi-bridge CLI.

pi-bridge submit   [--cwd DIR] [--task TEXT | --task-file F]  (default: task on stdin)
                   [--origin-platform P] [--origin-chat-id ID] [--origin-thread-id T]
                   [--origin-ui-session-id S]   (delivery origin metadata; invalid
                   values are dropped, never an error; the bridge never routes on it)
pi-bridge status   JOB_ID [--json] [--full]
pi-bridge feedback JOB_ID [--feedback TEXT | --feedback-file F]  (default: on stdin)
pi-bridge cancel   JOB_ID [--json]
pi-bridge list     [--limit N] [--json]
pi-bridge web-info JOB_ID   (read-only PI WEB observability view, JSON)
"""
from __future__ import annotations

import argparse
import json
import sys

from . import bridge, state
from .state import BridgeError


def _emit(job: dict, as_json: bool, full: bool = False) -> None:
    if as_json:
        print(json.dumps(bridge.status_view(job, full=full), ensure_ascii=False))
    else:
        v = bridge.status_view(job, full=full)
        for k in ("job_id", "status", "created_at", "updated_at", "cwd",
                  "pi_session_id", "error", "final_result_truncated", "wake",
                  "origin"):
            if v.get(k) is None:
                continue
            val = v[k]
            if isinstance(val, (dict, list)):
                val = json.dumps(val, ensure_ascii=False)
            print(f"{k}: {val}")
        if v.get("final_result") is not None:
            print("--- final_result ---")
            print(v["final_result"])


def _read_text(text: str | None, file: str | None, stdin_label: str) -> str:
    if text is not None and file is not None:
        raise BridgeError("pass either text or file, not both")
    if file is not None:
        try:
            return open(file, "r", encoding="utf-8").read()
        except OSError as e:
            raise BridgeError(f"cannot read {file}: {e}") from None
    if text is not None:
        return text
    if sys.stdin.isatty():
        raise BridgeError(f"no {stdin_label} given (stdin is a tty; use --task/--feedback or a file flag)")
    return sys.stdin.read()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pi-bridge")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("submit", help="submit a task to the Pi orchestrator")
    sp.add_argument("--cwd", default=".", help="working directory for the Pi job")
    sp.add_argument("--task", default=None)
    sp.add_argument("--task-file", default=None)
    sp.add_argument("--pi-bin", default=None, help="path to the pi executable")
    sp.add_argument("--session-id", default=None,
                    help="explicit Pi session id (default: generated uuid)")
    sp.add_argument("--origin-platform", default="",
                    help="platform of the requesting session (e.g. telegram, webui)")
    sp.add_argument("--origin-chat-id", default="",
                    help="chat id of the requesting session")
    sp.add_argument("--origin-thread-id", default="",
                    help="thread/topic id of the requesting session (optional)")
    sp.add_argument("--origin-ui-session-id", default="",
                    help="UI session id for webui-originated requests (optional)")
    sp.add_argument("--json", action="store_true")

    st = sub.add_parser("status", help="show job status")
    st.add_argument("job_id")
    st.add_argument("--json", action="store_true")
    st.add_argument("--full", action="store_true", help="do not truncate final result")

    fb = sub.add_parser("feedback", help="send feedback to the same Pi session")
    fb.add_argument("job_id")
    fb.add_argument("--feedback", default=None)
    fb.add_argument("--feedback-file", default=None)
    fb.add_argument("--json", action="store_true")

    ins = sub.add_parser("install", help="detect pi binary, capability-check it, record it in bridge config")
    ins.add_argument("--pi-bin", default="", help="explicit pi path (default: PI_BRIDGE_PI_BIN then PATH)")
    ins.add_argument("--json", action="store_true")

    cn = sub.add_parser("cancel", help="cancel a running bridge job")
    cn.add_argument("job_id")
    cn.add_argument("--json", action="store_true")

    ls = sub.add_parser("list", help="list recent jobs (newest first)")
    ls.add_argument("--limit", type=int, default=bridge.LIST_LIMIT_DEFAULT,
                    help="max rows, newest first (default: "
                         f"{bridge.LIST_LIMIT_DEFAULT})")
    ls.add_argument("--json", action="store_true")

    wi = sub.add_parser("web-info",
                        help="read-only PI WEB observability view of a job")
    wi.add_argument("job_id")
    wi.add_argument("--json", action="store_true",
                    help="accepted for symmetry; output is always JSON")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "submit":
            task = _read_text(args.task, args.task_file, "task")
            origin = bridge.clean_origin(
                platform=args.origin_platform,
                chat_id=args.origin_chat_id,
                thread_id=args.origin_thread_id,
                ui_session_id=args.origin_ui_session_id,
            )
            job = bridge.submit(task, args.cwd, pi_bin=args.pi_bin,
                                session_id=args.session_id, origin=origin)
            if args.json:
                print(json.dumps(bridge.status_view(job), ensure_ascii=False))
            else:
                print(job["job_id"])
        elif args.cmd == "status":
            job = state.load_job(args.job_id)
            job = bridge.reconcile(job)
            _emit(job, args.json, full=args.full)
        elif args.cmd == "feedback":
            text = _read_text(args.feedback, args.feedback_file, "feedback")
            job = bridge.feedback(args.job_id, text)
            if args.json:
                print(json.dumps(bridge.status_view(job), ensure_ascii=False))
            else:
                print(job["job_id"])
        elif args.cmd == "cancel":
            job = bridge.cancel(args.job_id)
            if args.json:
                print(json.dumps(bridge.status_view(job), ensure_ascii=False))
            else:
                print(f"{job['job_id']}: {job['status']}")
        elif args.cmd == "web-info":
            job = state.load_job(args.job_id)
            print(json.dumps(bridge.piweb_view(job), ensure_ascii=False))
        elif args.cmd == "install":
            import os as _os
            import shutil as _shutil
            from pathlib import Path as _Path
            cand = (args.pi_bin or _os.environ.get("PI_BRIDGE_PI_BIN") or "").strip() \
                or (_shutil.which("pi") or "")
            if not cand:
                raise BridgeError(
                    "pi executable not found. Install Pi (or pass --pi-bin / export PI_BRIDGE_PI_BIN). "
                    "The installer does NOT install or update Pi automatically.")
            p = _Path(cand).expanduser()
            if not p.is_absolute():
                found = _shutil.which(str(p))
                if not found:
                    raise BridgeError(f"pi executable not found: {cand}")
                p = _Path(found)
            p = p.resolve()
            if not p.is_file() or not _os.access(p, _os.X_OK):
                raise BridgeError(f"pi executable not usable: {p}")
            version = bridge.check_pi_capability(str(p))
            cfg_path = state.bridge_home() / "config.json"
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            existing = bridge.bridge_config()
            existing["pi_bin"] = str(p)
            existing["pi_version"] = version
            state.write_text_atomic(cfg_path, json.dumps(existing, indent=2) + "\n")
            out = {"ok": True, "pi_bin": str(p), "pi_version": version,
                   "config": str(cfg_path)}
            if args.json:
                print(json.dumps(out, ensure_ascii=False))
            else:
                print(f"recorded pi: {p} ({version})\nconfig: {cfg_path}")

        elif args.cmd == "list":
            jobs = [bridge.reconcile(state.load_job(j["job_id"]))
                    for j in state.list_jobs()]
            rows = bridge.list_rows(jobs, limit=args.limit)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False))
            else:
                for r in rows:
                    woken = ("wake:yes" if r["wake"]["delivered"]
                             else ("wake:no" if r["wake"]["enabled"] else "wake:off"))
                    print(f"{r['job_id']}  {r['status']:<12}  "
                          f"{r['updated_at']}  turns={r['turns']} "
                          f"fb={r['feedback_turns']} {woken:<8} {r['cwd']}")
    except BridgeError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
