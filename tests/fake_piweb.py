"""Minimal fake PI WEB web service for tests (read-only endpoints + the one
documented POST /api/projects).  Runs on a background thread on 127.0.0.1.

Unlike the real PI WEB it does not scan the session store: the sessions it
reports are supplied by the test, which keeps assertions deterministic.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _Server(ThreadingHTTPServer):
    """Quiet, threaded: a timed-out client closing its socket is normal here."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        app = self.server.app
        app.record("GET", self.path)
        if app.latency:
            time.sleep(app.latency)
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"
        if path == "/api/pi-web/status":
            return self._send(200, {"components": {"web": {
                "runtimeVersion": "test", "available": True}}})
        if path == "/api/projects":
            return self._send(200, app.snapshot_projects())
        m = re.fullmatch(r"/api/projects/([^/]+)/workspaces", path)
        if m:
            proj = app.project_by_id(m.group(1))
            if proj is None:
                return self._send(404, {"error": "unknown project"})
            return self._send(200, {
                "status": "folder", "projectId": proj["id"], "diagnostics": [],
                "workspaces": [{"id": "ws-" + proj["id"], "projectId": proj["id"],
                                "path": proj["path"], "label": proj["name"],
                                "isMain": True}]})
        if path == "/api/machines/local/sessions":
            return self._send(200, app.snapshot_sessions(q.get("cwd", [""])[0]))
        return self._send(404, {"error": "no such endpoint"})

    def do_POST(self):
        app = self.server.app
        app.record("POST", self.path)
        if app.latency:
            time.sleep(app.latency)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}
        if self.path.rstrip("/") == "/api/projects":
            proj = app.add_project(str(body.get("path") or ""))
            if proj is None:
                return self._send(403, {"error": "path is not allowed"})
            return self._send(201, proj)
        return self._send(404, {"error": "no such endpoint"})


class FakePiWeb:
    def __init__(self, allowed_roots, projects=None, sessions=None,
                 latency: float = 0.0, register_mode: str = "ok"):
        # register_mode: "ok" | "refuse" (403) | "ghost" (2xx but never listed)
        self.register_mode = register_mode
        self.allowed = [str(Path(r).resolve()) for r in allowed_roots]
        self._projects = [dict(p) for p in (projects or [])]
        self._sessions = {str(Path(k).resolve()): [dict(s) for s in v]
                          for k, v in (sessions or {}).items()}
        self.latency = latency
        self.requests: list[tuple[str, str]] = []
        self._seq = 0
        self._lock = threading.Lock()
        self._srv = _Server(("127.0.0.1", 0), _Handler)
        self._srv.app = self
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        daemon=True)

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=5)

    @property
    def base(self) -> str:
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}"

    def config(self, tmp_path) -> str:
        """Write a PI WEB config file for this server; return its path."""
        cfg = {"host": "127.0.0.1", "port": self._srv.server_address[1],
               "pathAccess": {"allowedPaths": self.allowed}}
        p = Path(tmp_path) / "pi-web-config.json"
        p.write_text(json.dumps(cfg))
        return str(p)

    # -- inspection --------------------------------------------------------
    def record(self, method, path):
        with self._lock:
            self.requests.append((method, path))

    def count(self, method, path_prefix) -> int:
        with self._lock:
            return sum(1 for m, p in self.requests
                       if m == method and p.startswith(path_prefix))

    # -- state -------------------------------------------------------------
    def snapshot_projects(self):
        with self._lock:
            return [dict(p) for p in self._projects]

    def project_by_id(self, pid):
        with self._lock:
            for p in self._projects:
                if p["id"] == pid:
                    return dict(p)
        return None

    def add_project(self, path: str):
        real = str(Path(path).resolve()) if path else ""
        if not any(real == r or real.startswith(r.rstrip("/") + "/")
                   for r in self.allowed):
            return None
        if self.register_mode == "refuse":
            return None
        if self.register_mode == "ghost":
            return {"id": "ghost", "name": Path(real).name, "path": real,
                    "createdAt": "2026-01-01T00:00:00.000Z"}
        with self._lock:
            for p in self._projects:
                if p["path"] == real:      # idempotent, like the real one
                    return dict(p)
            self._seq += 1
            proj = {"id": f"proj-{self._seq}",
                    "name": Path(real).name or "root",
                    "path": real,
                    "createdAt": "2026-01-01T00:00:00.000Z"}
            self._projects.append(proj)
            return dict(proj)

    def set_sessions(self, cwd, sessions):
        with self._lock:
            self._sessions[str(Path(cwd).resolve())] = [dict(s) for s in sessions]

    def snapshot_sessions(self, cwd):
        with self._lock:
            return [dict(s) for s in self._sessions.get(str(Path(cwd).resolve()), [])]


def session_row(session_id, message_count=1, modified="2026-01-01T00:00:00.000Z"):
    return {"id": session_id, "path": "/dev/null", "cwd": "", "persisted": True,
            "created": "2026-01-01T00:00:00.000Z", "modified": modified,
            "messageCount": message_count, "firstMessage": "hello"}
