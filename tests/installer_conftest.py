"""Sandbox fixtures for installer/uninstaller e2e tests.

Everything runs against a temporary HOME + PI_BRIDGE_HOME + HERMES_HOME under
/tmp — production state is never referenced.  `hermes` and `pi` are the fakes
in tests/fakes/bin.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FACKS = REPO / "tests" / "fakes"

EXTRA_ROUTE_YAML = """\
model: some/model
platforms:
  webhook:
    enabled: true
    extra:
      host: 127.0.0.1
      port: 8644
      secret: foreign-global-secret
      routes:
        other-team-route:
          secret: foreign-route-secret
          events: [something.else]
          deliver: telegram
"""


class Sandbox:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.hermes = self.home / ".hermes"
        self.bridge = tmp / "bridge-home"
        self.pi_agent = self.home / ".pi" / "agent"
        self.repo = tmp / "checkout"
        for d in (self.home / ".local" / "bin", self.hermes, self.pi_agent,
                  self.bridge, tmp / "bin"):
            d.mkdir(parents=True, exist_ok=True)
        # fakes on PATH
        for f in ("hermes", "pi"):
            src = FACKS / "bin" / f
            dst = tmp / "bin" / f
            shutil.copy(src, dst)
            dst.chmod(0o755)
        # the fake `pi` finds its turn-emulator next to itself or in ../fakes
        shutil.copy(FACKS / "fake_agent.py", tmp / "fake_agent.py")
        shutil.copy(FACKS / "fake_agent.py", tmp / "bin" / "fake_agent.py")
        # isolated checkout WITHOUT .venv (installer creates it)
        shutil.copytree(REPO, self.repo, ignore=shutil.ignore_patterns(
            ".venv", "__pycache__", ".pytest_cache", "*.egg-info"))
        self.soul = self.hermes / "SOUL.md"
        self.cfg = self.hermes / "config.yaml"
        self.wake = self.bridge / "wake.json"

    def env(self, **overrides) -> dict:
        e = os.environ.copy()
        e.update({
            "HOME": str(self.home),
            "HERMES_HOME": str(self.hermes),
            "PI_BRIDGE_HOME": str(self.bridge),
            "PI_CODING_AGENT_DIR": str(self.pi_agent),
            "XDG_RUNTIME_DIR": "",          # no systemd user session in sandbox
            "PI_BRIDGE_SKIP_TESTS": "1",
            "PYTHONPATH": "",               # no leakage; editable install handles imports
        })
        # HARD isolation: PATH contains ONLY the sandbox fakes + coreutils.
        # Never inherit the caller PATH: it contains a real `hermes`/`pi`, and
        # the installer would then run real production binaries (v0.2 does a
        # controlled gateway restart!).
        e["PATH"] = f"{self.tmp}/bin:/usr/bin:/bin"
        e.update({k: str(v) for k, v in overrides.items()})
        return e

    def install(self, *args, env_extra=None, input="", timeout=600):
        env = self.env(**(env_extra or {}))
        return subprocess.run(
            ["bash", str(self.repo / "install.sh"), "--yes", *args],
            cwd=self.repo, env=env, input=input, capture_output=True,
            text=True, timeout=timeout)

    def uninstall(self, env_extra=None, input="n\nn\n", timeout=120):
        env = self.env(**(env_extra or {}))
        return subprocess.run(
            ["bash", str(self.repo / "uninstall.sh")],
            cwd=self.repo, env=env, input=input, capture_output=True,
            text=True, timeout=timeout)

    def seed_soul(self, text="You are Hermes.\nmy precious custom soul text\n"):
        self.soul.write_text(text)

    def seed_cfg(self, text=EXTRA_ROUTE_YAML):
        self.cfg.write_text(text)

    def seed_orchestrator(self):
        (self.pi_agent / "agents").mkdir(parents=True, exist_ok=True)
        (self.pi_agent / "agents" / "orchestrator.md").write_text(
            "---\nname: orchestrator\nmode: primary\n---\nmine\n")

    def seed_open_agents(self):
        (self.pi_agent / "settings.json").write_text(
            '{"packages": ["npm:pi-open-agents"]}')


@pytest.fixture()
def sandbox(tmp_path) -> Sandbox:
    return Sandbox(tmp_path)


def health_server(tmp: Path, port: int):
    """Background one-shot fake webhook endpoint so the installer's post
    restart port check succeeds (returns the Popen)."""
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import http.server,socketserver,sys;"
         "socketserver.TCPServer.allow_reuse_address=True;"
         "s=socketserver.TCPServer(('127.0.0.1',int(sys.argv[1])),http.server.SimpleHTTPRequestHandler);"
         "s.handle_request();s.handle_request()" , str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc
