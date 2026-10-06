"""End-to-end tests for the v0.2 guided installer / uninstaller.

Every scenario runs `install.sh` / `uninstall.sh` in a copied checkout with a
fully sandboxed HOME / PI_BRIDGE_HOME / HERMES_HOME / PI_CODING_AGENT_DIR
under /tmp and fake `hermes` + `pi` binaries on PATH.  Production state is
never referenced: the fakes only ever write inside the sandbox, and the
assertions read sandbox files exclusively.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from installer_conftest import Sandbox, health_server, sandbox  # noqa: F401  (pytest fixture)


def yaml_load(path: Path):
    import io
    from ruamel.yaml import YAML
    return YAML(typ="safe").load(io.StringIO(path.read_text()))


def route_of(sandbox: Sandbox) -> dict:
    doc = yaml_load(sandbox.cfg)
    return doc["platforms"]["webhook"]["extra"]["routes"]["pi-bridge-complete"]


def wait_first(sandbox, pattern):  # unused helper kept for clarity
    ...


# ---------------------------------------------------------------- happy path

def test_first_install_full_guided_setup(sandbox):
    sandbox.seed_soul()
    sandbox.seed_cfg()
    r = sandbox.install()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pi-hermes-bridge is ready" in r.stdout

    # SOUL: managed block appended, foreign text preserved
    soul = sandbox.soul.read_text()
    assert "<!-- pi-hermes-bridge:begin -->" in soul
    assert "my precious custom soul text" in soul
    assert soul.count("<!-- pi-hermes-bridge:begin -->") == 1

    # config.yaml: our route added, foreign route + global secret untouched
    doc = yaml_load(sandbox.cfg)
    routes = doc["platforms"]["webhook"]["extra"]["routes"]
    assert set(routes) == {"other-team-route", "pi-bridge-complete"}
    assert routes["other-team-route"]["secret"] == "foreign-route-secret"
    assert doc["platforms"]["webhook"]["extra"]["secret"] == "foreign-global-secret"
    rt = routes["pi-bridge-complete"]
    assert rt["deliver"] == "log"
    assert rt["events"] == ["pi_bridge_turn_complete"]
    assert "pi_bridge" in rt["toolsets"]

    # wake.json: written, 0600, loopback URL, same secret as the route
    wake = json.loads(sandbox.wake.read_text())
    assert wake["enabled"] is True
    assert wake["url"] == "http://127.0.0.1:8644/webhooks/pi-bridge-complete"
    assert wake["secret"] == rt["secret"] and len(wake["secret"]) >= 32
    assert stat.S_IMODE(sandbox.wake.stat().st_mode) == 0o600

    # plugin copied + enabled; all 5 tools; ownership recorded
    assert (sandbox.hermes / "plugins" / "pi-worker" / "plugin.yaml").is_file()
    assert (sandbox.hermes / "enabled_pi-worker").is_file()
    manifest = (sandbox.hermes / "plugins" / "pi-worker" / "plugin.yaml").read_text()
    for t in ("pi_delegate", "pi_status", "pi_feedback", "pi_cancel", "pi_list"):
        assert t in manifest
    state = json.loads((sandbox.bridge / "hermes_setup.json").read_text())
    assert state["plugin_installed"] is True
    assert state["wake"]["route_owned"] is True

    # pi-open-agents installed WITH consent + orchestrator created, no model
    assert "npm:pi-open-agents" in (sandbox.pi_agent / "settings.json").read_text()
    orch = (sandbox.pi_agent / "agents" / "orchestrator.md").read_text()
    assert "name: orchestrator" in orch
    assert "allowedAgents: [explorer, worker, reviewer]" in orch
    assert not re.search(r"^model:", orch, re.M)  # no model prescribed

    # CLI linked for the gateway, pi recorded in bridge config
    link = sandbox.home / ".local" / "bin" / "pi-bridge"
    assert link.is_symlink() and link.resolve().exists()
    assert json.loads((sandbox.bridge / "config.json").read_text())["pi_bin"]

    # calls log: plugin enabled through the official CLI
    calls = (sandbox.hermes / "calls.log").read_text()
    assert "plugins enable pi-worker" in calls


def test_second_install_is_idempotent(sandbox):
    sandbox.seed_soul()
    sandbox.seed_cfg()
    assert sandbox.install().returncode == 0
    cfg1 = sandbox.cfg.read_text()
    soul1 = sandbox.soul.read_text()
    r2 = sandbox.install()
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert sandbox.cfg.read_text() == cfg1, "config.yaml changed on re-install"
    assert sandbox.soul.read_text() == soul1
    soul = sandbox.soul.read_text()
    assert soul.count("pi-hermes-bridge:begin") == 1
    doc = yaml_load(sandbox.cfg)
    routes = doc["platforms"]["webhook"]["extra"]["routes"]
    assert set(routes) == {"other-team-route", "pi-bridge-complete"}
    # no second backup file clobbering the first
    assert (sandbox.hermes / "config.yaml.pi-hermes-bridge.bak").is_file()
    assert not (sandbox.hermes / "config.yaml.pi-hermes-bridge.bak.1").exists()
    # the same secret is reused, never re-generated
    wake = json.loads(sandbox.wake.read_text())
    assert wake["secret"] == routes["pi-bridge-complete"]["secret"]


# ------------------------------------------------------------------ upgrades

def test_upgrade_from_manual_v01_route_no_duplicates(sandbox):
    sandbox.seed_cfg()
    sandbox.cfg.write_text(sandbox.cfg.read_text().replace(
        "model: some/model\n", """\
manual-note: keep me
""") + """\
        pi-bridge-complete:
          secret: manual-v01-secret
          events: [pi_bridge_turn_complete]
          deliver: log
          prompt: OLD MANUAL V0.1 PROMPT
""")
    sandbox.wake.parent.mkdir(parents=True, exist_ok=True)
    sandbox.wake.write_text(json.dumps(
        {"enabled": True, "url": "http://127.0.0.1:8644/webhooks/pi-bridge-complete",
         "secret": "manual-v01-secret"}))
    r = sandbox.install()
    assert r.returncode == 0, r.stdout + r.stderr
    doc = yaml_load(sandbox.cfg)
    routes = doc["platforms"]["webhook"]["extra"]["routes"]
    # exactly one pi-bridge-complete route (no second webhook created),
    # the manual one is left EXACTLY as it was, foreign text preserved
    assert set(routes) == {"other-team-route", "pi-bridge-complete"}
    assert routes["pi-bridge-complete"]["prompt"] == "OLD MANUAL V0.1 PROMPT"
    assert "manual-note: keep me" in sandbox.cfg.read_text()
    # existing manual wake.json untouched (secret still matches the route)
    assert json.loads(sandbox.wake.read_text())["secret"] == "manual-v01-secret"
    assert "kept_manual" in r.stdout or "LEFT UNTOUCHED" in r.stdout


def test_upgrade_wake_json_missing_reuses_manual_route_secret(sandbox):
    sandbox.seed_cfg()
    sandbox.cfg.write_text(sandbox.cfg.read_text() + """\
        pi-bridge-complete:
          secret: manual-v01-secret
          events: [pi_bridge_turn_complete]
          deliver: log
""")
    r = sandbox.install()
    assert r.returncode == 0, r.stdout + r.stderr
    wake = json.loads(sandbox.wake.read_text())
    assert wake["secret"] == "manual-v01-secret"


# ------------------------------------------------------------ prerequisites

def test_missing_hermes_stops_with_url(sandbox):
    os.remove(sandbox.tmp / "bin" / "hermes")
    r = sandbox.install()
    assert r.returncode != 0
    assert "does NOT install Hermes" in r.stderr
    assert "https://github.com/NousResearch/hermes-agent" in r.stderr
    # nothing was installed
    assert not (sandbox.repo / ".venv").exists()
    assert not (sandbox.hermes / "plugins").exists()


def test_missing_pi_stops_with_url(sandbox):
    os.remove(sandbox.tmp / "bin" / "pi")
    r = sandbox.install()
    assert r.returncode != 0
    assert "does NOT install Pi" in r.stderr
    assert "https://github.com/earendil-works/pi" in r.stderr
    assert not (sandbox.repo / ".venv").exists()


def test_pi_without_agent_support_stops(sandbox):
    r = sandbox.install(env_extra={"FAKE_PI_NO_AGENT": 1})
    assert r.returncode != 0
    assert "--agent" in r.stderr
    assert "pi install npm:pi-open-agents" in r.stderr


def test_pi_open_agents_install_offered_and_declined(sandbox):
    # decline: answers come from stdin, so run install.sh manually without --yes
    env = sandbox.env()
    r = subprocess.run(["bash", str(sandbox.repo / "install.sh")],
                       cwd=sandbox.repo, env=env, input="n\n",
                       capture_output=True, text=True, timeout=300)
    assert r.returncode != 0
    assert "pi install npm:pi-open-agents" in r.stdout   # shown WHAT is installed
    assert not (sandbox.pi_agent / "installs.log").exists()
    assert not (sandbox.hermes / "plugins").exists()     # nothing partial


def test_existing_compatible_orchestrator_untouched(sandbox):
    sandbox.seed_orchestrator()
    sandbox.seed_open_agents()
    r = sandbox.install()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Existing orchestrator agent found" in r.stdout
    assert (sandbox.pi_agent / "agents" / "orchestrator.md").read_text() == \
        "---\nname: orchestrator\nmode: primary\n---\nmine\n"
    assert not (sandbox.pi_agent / "installs.log").exists()  # no reinstall


def test_equivalent_extension_plus_orchestrator_no_offers(sandbox):
    # --agent provided by some *equivalent* extension (no pi-open-agents
    # marker anywhere) + an existing orchestrator: requirement §2 says
    # "compatible extension + orchestrator -> change nothing".
    sandbox.seed_orchestrator()
    r = sandbox.install()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Install pi-open-agents" not in r.stdout
    assert not (sandbox.pi_agent / "installs.log").exists()
    assert (sandbox.pi_agent / "agents" / "orchestrator.md").read_text() == \
        "---\nname: orchestrator\nmode: primary\n---\nmine\n"


# ------------------------------------------------------------------- PATH

def test_hostile_reduced_path(sandbox):
    sandbox.seed_soul()
    sandbox.seed_cfg()
    env = sandbox.env()
    env["PATH"] = f"{sandbox.tmp}/bin:/usr/bin:/bin"
    r = subprocess.run(["bash", str(sandbox.repo / "install.sh"), "--yes"],
                       cwd=sandbox.repo, env=env, input="",
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    assert route_of(sandbox)["deliver"] == "log"


# ------------------------------------------------- install -> uninstall

def test_uninstall_survives_lost_ownership_state(sandbox):
    sandbox.seed_soul()
    sandbox.seed_cfg()
    assert sandbox.install().returncode == 0
    (sandbox.bridge / "hermes_setup.json").unlink()   # state lost/relocated
    r = sandbox.uninstall(input="n\nn\n")
    assert r.returncode == 0, r.stdout + r.stderr
    # marker-based removal still works
    assert "pi-hermes-bridge" not in sandbox.soul.read_text()
    # route-level removal refuses to guess without ownership -> foreign kept + hint
    doc = yaml_load(sandbox.cfg)
    assert "pi-bridge-complete" in doc["platforms"]["webhook"]["extra"]["routes"]
    assert "other-team-route" in doc["platforms"]["webhook"]["extra"]["routes"]
    assert "PI_BRIDGE_HOME" in r.stdout


def test_install_refuses_foreign_wake_json_that_cannot_authenticate(sandbox):
    sandbox.seed_cfg()
    sandbox.wake.write_text('{"enabled": true, "url": "http://127.0.0.1:9999/other", "secret": "stale"}')
    r = sandbox.install()
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "wake.json" in out and "problem" in out
    assert "pi-hermes-bridge is ready" not in out
    # the foreign wake.json was left exactly as-is for the operator to review
    assert "stale" in sandbox.wake.read_text()

def test_install_then_uninstall_roundtrip(sandbox):
    sandbox.seed_soul()
    sandbox.seed_cfg()
    assert sandbox.install().returncode == 0
    soul_user = sandbox.soul.read_text().index("my precious custom soul text")
    r = sandbox.uninstall(input="n\nn\n")   # keep state, keep checkout
    assert r.returncode == 0, r.stdout + r.stderr

    soul = sandbox.soul.read_text()
    assert "pi-hermes-bridge" not in soul
    assert "my precious custom soul text" in soul
    doc = yaml_load(sandbox.cfg)
    routes = doc["platforms"]["webhook"]["extra"]["routes"]
    assert set(routes) == {"other-team-route"}       # only OUR route removed
    assert doc["platforms"]["webhook"]["extra"]["secret"] == "foreign-global-secret"
    assert not sandbox.wake.exists()
    assert not (sandbox.hermes / "plugins" / "pi-worker").exists()
    assert not (sandbox.home / ".local" / "bin" / "pi-bridge").exists()
    assert (sandbox.bridge).exists()                  # state kept on "n"
    # pi-open-agents survives uninstall even though the installer installed it
    assert "npm:pi-open-agents" in (sandbox.pi_agent / "settings.json").read_text()
    assert (sandbox.pi_agent / "agents" / "orchestrator.md").is_file()
    calls = (sandbox.hermes / "calls.log").read_text()
    assert "plugins disable pi-worker" in calls and "plugins remove pi-worker" in calls
    assert soul_user is not None


def test_second_install_performs_no_gateway_restart(sandbox):
    # fake hermes reports the gateway as running; first install must restart
    # exactly once, a no-op re-install must NOT restart again (requirement §5:
    # one controlled restart *if needed*)
    sandbox.seed_soul()
    sandbox.seed_cfg()
    assert sandbox.install().returncode == 0
    log = sandbox.hermes / "gateway_restart.log"
    assert log.exists() and len(log.read_text().splitlines()) == 1
    r2 = sandbox.install()
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "no gateway restart needed" in r2.stdout
    assert len(log.read_text().splitlines()) == 1


def test_manual_upgrade_disabled_wake_json_warns_not_ready(sandbox):
    # v0.1 manual route + a disabled wake.json matching it: the installer must
    # not silently claim 'ready' (wakes would never fire)
    sandbox.seed_cfg()
    sandbox.cfg.write_text(sandbox.cfg.read_text() + """\
        pi-bridge-complete:
          secret: manual-v01-secret
          events: [pi_bridge_turn_complete]
          deliver: log
""")
    sandbox.wake.write_text(json.dumps(
        {"enabled": False,
         "url": "http://127.0.0.1:8644/webhooks/pi-bridge-complete",
         "secret": "manual-v01-secret"}))
    r = sandbox.install()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "is ready" not in r.stdout
    assert "does not match the manual route" in r.stdout
    # nothing changed about the operator's file
    assert json.loads(sandbox.wake.read_text())["enabled"] is False


# --------------------------------------------------- route prompt content

def test_wake_route_prompt_is_origin_aware(sandbox):
    assert sandbox.install().returncode == 0
    prompt = route_of(sandbox)["prompt"]
    assert "origin" in prompt
    assert "hermes send -t" in prompt
    assert "timeout 8" in prompt and "SESSION_NOT_OWNED" in prompt
    assert "do NOT fall back to Telegram" in prompt
    assert "untrusted data" in prompt
    assert route_of(sandbox)["deliver"] == "log"
    # no machine-specific constants leaked into the generated route
    assert "123456789" not in prompt
    assert "telegram:123" not in prompt
