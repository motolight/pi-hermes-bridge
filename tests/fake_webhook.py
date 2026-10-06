#!/usr/bin/env python3
"""Local HMAC-verifying stand-in for the Hermes webhook platform.

Implements the SAME generic HMAC V2 verification as
``gateway/platforms/webhook.py`` (lines 620-670 of the gateway source), so a
test that passes here passes the real gateway:

    X-Webhook-Timestamp:    integer unix seconds, accepted within +-300 s
    X-Webhook-Signature-V2: hex HMAC-SHA256(secret, "<timestamp>." + raw_body)
    Content-Type:           application/json

Scheme dispatch is mirrored too: if a request carries svix / Standard-
Webhooks / GitHub / GitLab / Linear signature headers, verification is routed
into that scheme instead of V2 (the tests never send those, and their absence
is asserted).

Behaviour knobs (constructor args or CLI flags for the standalone smoke):
    responses     list of statuses to answer, repeating the LAST one
                  (e.g. [500] answers 500 forever, [503, 503, 202] answers
                  503, 503, then 202 forever)
    require_sig   reject a bad/missing signature with 401 (default True)
    events        every accepted delivery: {"headers", "body", ...}

Run standalone (real-stack smoke, loopback only):

    python tests/fake_webhook.py --port 8655 --events-file /tmp/events.jsonl
    # secret is read from WAKE_TEST_SECRET (never from argv)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

V2_REPLAY_WINDOW_SECONDS = 300  # same constant as webhook.py:60
SIG_HEADER = "X-Webhook-Signature-V2"
TS_HEADER = "X-Webhook-Timestamp"
# Headers that would move verification to another scheme (webhook.py:632-643).
OTHER_SCHEME_HEADERS = ("svix-id", "svix-timestamp", "svix-signature",
                        "webhook-id", "webhook-timestamp", "webhook-signature",
                        "linear-signature", "X-Hub-Signature-256",
                        "X-Gitlab-Token")


def verify_v2(secret: str, headers: dict, body: bytes) -> tuple[bool, str]:
    """(ok, reason) using the gateway's own V2 rule."""
    get = lambda k: next((v for kk, v in headers.items()
                          if kk.lower() == k.lower()), "")
    sig = get(SIG_HEADER)
    if not sig:
        return False, "missing signature header"
    ts = get(TS_HEADER)
    if not ts:
        # V2 commits to V2: no fall-back to legacy V1 (webhook.py:652-655)
        return False, "V2 signature without timestamp"
    try:
        age = abs(int(time.time()) - int(ts))
    except (TypeError, ValueError):
        return False, "unparseable timestamp"
    if age > V2_REPLAY_WINDOW_SECONDS:
        return False, f"timestamp outside replay window (age={age}s)"
    expected = hmac.new(secret.encode(), ts.encode() + b"." + body,
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig.encode(), expected.encode()):
        return False, "signature mismatch"
    return True, "ok"


class Receiver(ThreadingHTTPServer):
    """Single-threaded-in-spirit but concurrent-safe HTTP receiver."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, secret: str, responses=None,
                 require_sig: bool = True, events_file: str | None = None):
        super().__init__(addr, _Handler)
        self.secret = secret
        self.responses = list(responses) if responses else [202]
        self.require_sig = require_sig
        self.events_file = events_file
        self.lock = threading.Lock()
        self.requests = []      # every request, valid or not
        self.events = []        # deliveries that passed verification
        self.rejected = []      # deliveries rejected by verification

    def next_status(self) -> int:
        with self.lock:
            idx = min(len(self.requests), len(self.responses) - 1)
            return self.responses[idx]

    def record(self, entry: dict, accepted: bool) -> None:
        with self.lock:
            self.requests.append(entry)
            (self.events if accepted else self.rejected).append(entry)
            if self.events_file and accepted:
                with open(self.events_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence stderr spam
        pass

    def _reply(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        headers = {k: v for k, v in self.headers.items()}
        srv: Receiver = self.server
        ok, reason = verify_v2(srv.secret, headers, raw)
        other = [h for h in OTHER_SCHEME_HEADERS
                 if any(k.lower() == h.lower() for k in headers)]
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        entry = {
            "path": self.path,
            "headers": headers,
            "body": payload,
            "raw_b64": base64.b64encode(raw).decode(),
            "raw_len": len(raw),
            "verified": ok,
            "verify_reason": reason,
            "other_scheme_headers": other,
            "received_at": time.time(),
        }
        if srv.require_sig and not ok:
            srv.record(entry, accepted=False)
            return self._reply(401, {"error": "Invalid signature"})
        status = srv.next_status()
        srv.record(entry, accepted=True)
        if status == 202:
            return self._reply(202, {"status": "accepted",
                                     "delivery_id": headers.get("X-Request-ID")})
        if status == 200:
            return self._reply(200, {"status": "duplicate"})
        return self._reply(status, {"error": "simulated failure"})


def start_receiver(port: int = 0, secret: str = "test-secret", **kw) -> Receiver:
    srv = Receiver(("127.0.0.1", port), secret, **kw)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1},
                     daemon=True).start()
    return srv


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="fake_webhook")
    ap.add_argument("--port", type=int, default=8655)
    ap.add_argument("--events-file", default=None)
    ap.add_argument("--responses", default="202",
                    help="comma-separated statuses; the last one repeats")
    args = ap.parse_args()
    secret = os.environ.get("WAKE_TEST_SECRET", "")
    if not secret:
        print("WAKE_TEST_SECRET must be set", flush=True)
        return 2
    srv = Receiver(("127.0.0.1", args.port), secret,
                   responses=[int(x) for x in args.responses.split(",")],
                   events_file=args.events_file)
    print(f"listening 127.0.0.1:{srv.server_address[1]} "
          f"webhooks/pi-bridge-complete", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
