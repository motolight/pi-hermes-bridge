"""Unit tests for pi_bridge.hermes_setup (managed SOUL block, wake route,
ownership state, wake.json).  All paths sandboxed via env vars."""
from __future__ import annotations

import json
import os
import stat

import pytest

from pi_bridge import hermes_setup as hs
from pi_bridge.state import BridgeError

FOREIGN_CFG = """\
model: some/model
platforms:
  webhook:
    enabled: true
    extra:
      host: 127.0.0.1
      port: 9100
      secret: foreign-global
      routes:
        other-team-route:  # a comment that must survive
          secret: foreign-secret
          events: [something.else]
          deliver: telegram
# trailing comment survives too
"""


@pytest.fixture()
def sb(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setenv("PI_BRIDGE_HOME", str(tmp_path / "bridge"))
    return tmp_path


# ---------------------------------------------------------------- SOUL block

def test_soul_insert_update_remove_roundtrip(sb):
    p = hs.soul_path()
    original = "You are Hermes.\nline two\n"
    p.write_text(original)
    r, action = hs.install_soul_block(original)
    assert action == "created"
    assert hs.SOUL_BEGIN in r and hs.SOUL_END in r
    assert r.startswith("You are Hermes.\nline two\n")
    # update replaces, never duplicates
    r2, action2 = hs.install_soul_block(r, SOUL2 := "NEW POLICY TEXT")
    assert action2 == "updated"
    assert r2.count(hs.SOUL_BEGIN) == 1
    assert "NEW POLICY TEXT" in r2 and hs.SOUL_POLICY not in r2
    # removal restores foreign text exactly (modulo trailing newline)
    r3, action3 = hs.remove_soul_block(r2)
    assert action3 == "removed"
    assert r3.strip() == original.strip()
    assert hs.SOUL_BEGIN not in r3


def test_soul_file_idempotent_and_backup_once(sb):
    p = hs.soul_path()
    p.write_text("soul original\n")
    assert hs.soul_install_file()["soul"] == "created"
    first = p.read_text()
    assert hs.soul_install_file()["soul"] == "unchanged"
    assert p.read_text() == first
    bak = p.with_name(p.name + ".pi-hermes-bridge.bak")
    assert bak.read_text() == "soul original\n"
    hs.soul_install_file()
    assert bak.read_text() == "soul original\n"  # backup never overwritten
    assert hs.soul_remove_file()["soul"] == "removed"
    assert p.read_text() == "soul original\n"


def test_soul_missing_file_and_half_marker(sb):
    assert hs.soul_install_file()["soul"] == "created"
    hs.soul_remove_file()
    hs.soul_path().write_text("x\n<!-- pi-hermes-bridge:begin -->\nleftover\n")
    with pytest.raises(BridgeError):
        hs.install_soul_block(hs.soul_path().read_text())


def test_soul_double_block_removal_refused(sb):
    t = hs._soul_block() + "\n\n" + hs._soul_block() + "\ntail\n"
    with pytest.raises(BridgeError):
        hs.remove_soul_block(t)
    # install onto an ambiguous layout must also refuse, not rewrite half
    with pytest.raises(BridgeError):
        hs.install_soul_block(t)
    # half marker (begin without end) is ambiguous too
    with pytest.raises(BridgeError):
        hs.install_soul_block("x\n" + hs.SOUL_BEGIN + "\nleftover\n")


# ------------------------------------------------------------ config routes

def _read_cfg():
    import io
    from ruamel.yaml import YAML
    y = YAML(typ="safe")
    return y.load(io.StringIO(hs.config_path().read_text()))


def test_route_fresh_platform_created(sb):
    r = hs.route_install("sec1", port=8644)
    assert r["route"] == "created_platform_and_route"
    doc = _read_cfg()
    wh = doc["platforms"]["webhook"]
    assert wh["enabled"] is True
    assert wh["extra"]["host"] == "127.0.0.1"
    assert wh["extra"]["port"] == 8644
    rt = wh["extra"]["routes"]["pi-bridge-complete"]
    assert rt["deliver"] == "log"
    assert rt["secret"] == "sec1"
    assert rt["events"] == ["pi_bridge_turn_complete"]
    assert "pi_bridge" in rt["toolsets"]


def test_route_into_existing_platform_preserves_everything(sb):
    hs.config_path().write_text(FOREIGN_CFG)
    r = hs.route_install("sec2")
    assert r["route"] == "created_route"
    assert r["port"] == 9100  # existing port wins over default
    text = hs.config_path().read_text()
    assert "a comment that must survive" in text
    assert "trailing comment survives too" in text
    doc = _read_cfg()
    assert doc["model"] == "some/model"
    routes = doc["platforms"]["webhook"]["extra"]["routes"]
    assert set(routes) == {"other-team-route", "pi-bridge-complete"}
    assert routes["other-team-route"]["secret"] == "foreign-secret"
    assert doc["platforms"]["webhook"]["extra"]["secret"] == "foreign-global"
    # update path keeps single route, refreshes prompt
    r2 = hs.route_install("sec2")
    assert r2["route"] == "updated_route"
    doc2 = _read_cfg()
    assert set(doc2["platforms"]["webhook"]["extra"]["routes"]) == set(routes)


def test_route_rejects_non_loopback(sb):
    hs.config_path().write_text(FOREIGN_CFG.replace("127.0.0.1", "0.0.0.0"))
    with pytest.raises(BridgeError):
        hs.route_install("s")
    # explicit override accepted
    r = hs.route_install("s", allow_non_loopback=True)
    assert r["route"] == "created_route"


def test_route_kept_manual_and_removal_ownership(sb):
    hs.config_path().write_text(FOREIGN_CFG + """\
        pi-bridge-complete:
          secret: manual-secret
          events: [pi_bridge_turn_complete]
          deliver: log
          prompt: old manual prompt
""")
    r = hs.route_install("s")
    assert r["route"] == "kept_manual"
    doc = _read_cfg()
    rt = doc["platforms"]["webhook"]["extra"]["routes"]["pi-bridge-complete"]
    assert rt["prompt"] == "old manual prompt"          # untouched
    assert rt["secret"] == "manual-secret"
    # removal: foreign route must survive
    rm = hs.route_remove()
    assert rm["route"] == "kept_foreign"
    assert "pi-bridge-complete" in _read_cfg()["platforms"]["webhook"]["extra"]["routes"]


def test_route_remove_only_owned_we_created_platform(sb):
    hs.route_install("s")
    assert hs.route_remove()["route"] == "removed"
    doc = _read_cfg()
    assert doc.get("platforms") is None  # we created platforms+webhook -> gone
    # foreign platform untouched:
    hs.save_state({"version": 1})   # fresh state, e.g. manual edit since
    hs.config_path().write_text(FOREIGN_CFG)
    hs.route_install("s")
    assert hs.route_remove()["route"] == "removed"
    doc = _read_cfg()
    routes = doc["platforms"]["webhook"]["extra"]["routes"]
    assert set(routes) == {"other-team-route"}
    assert doc["platforms"]["webhook"]["extra"]["secret"] == "foreign-global"


def test_route_disabled_foreign_platform_refused(sb):
    hs.config_path().write_text("""\
platforms:
  webhook:
    enabled: false
    extra:
      routes: {}
""")
    with pytest.raises(BridgeError):
        hs.route_install("s")


# ----------------------------------------------------------------- wake.json

def test_wake_write_perms_and_ownership(sb):
    hs.route_install("secw")
    r = hs.wake_write("secw", 8644)
    assert r["wake_json"] == "written"
    p = hs.wake_json_path()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    d = json.loads(p.read_text())
    assert d == {"enabled": True,
                 "url": "http://127.0.0.1:8644/webhooks/pi-bridge-complete",
                 "secret": "secw"}
    # a foreign wake.json is kept unless forced
    p.write_text('{"enabled": true, "url": "x", "secret": "manual"}')
    st = hs.load_state()
    st["wake"]["wake_json_owned"] = False
    hs.save_state(st)
    assert hs.wake_write("z", 8644)["wake_json"] == "kept_existing"
    assert json.loads(p.read_text())["secret"] == "manual"
    assert hs.wake_write("z", 8644, force=True)["wake_json"] == "written"
    # disable/remove
    assert hs.wake_disable(remove=True)["wake_json"] == "removed"
    assert not p.exists()


def test_wake_disable_keep_in_place(sb):
    hs.route_install("s")
    hs.wake_write("s", 8644)
    assert hs.wake_disable(remove=False)["wake_json"] == "disabled"
    assert json.loads(hs.wake_json_path().read_text())["enabled"] is False


def test_route_secret_in_config_helper(sb):
    hs.config_path().write_text(FOREIGN_CFG + """\
        pi-bridge-complete:
          secret: manual-secret
          events: [pi_bridge_turn_complete]
""")
    sec, port = hs.route_secret_in_config()
    assert (sec, port) == ("manual-secret", 9100)


# ------------------------------------------------------------------- skill

def test_skill_lifecycle(sb):
    repo = sb / "repo"
    (repo / "skills" / hs.SKILL_NAME).mkdir(parents=True)
    (repo / "skills" / hs.SKILL_NAME / "SKILL.md").write_text("skill body\n")
    assert hs.skill_install_file(repo)["skill"] == "created"
    assert (hs.skills_dir() / hs.SKILL_NAME / "SKILL.md").is_file()
    assert hs.skill_install_file(repo)["skill"] == "updated"
    # foreign skill dir with the same name is never overwritten
    import shutil
    shutil.rmtree(hs.skills_dir() / hs.SKILL_NAME)
    (hs.skills_dir() / hs.SKILL_NAME).mkdir()
    (hs.skills_dir() / hs.SKILL_NAME / "SKILL.md").write_text("foreign\n")
    assert hs.skill_install_file(repo)["skill"] == "skipped"
    assert (hs.skills_dir() / hs.SKILL_NAME / "SKILL.md").read_text() == "foreign\n"
    assert hs.skill_remove_file()["skill"] == "kept"
    # own copy removal
    (hs.skills_dir() / hs.SKILL_NAME / "SKILL.md").write_text("skill body\n")
    (hs.skills_dir() / hs.SKILL_NAME / ".pi-hermes-bridge").write_text("x")
    assert hs.skill_remove_file()["skill"] == "removed"
    assert not (hs.skills_dir() / hs.SKILL_NAME).exists()


# ------------------------------------------------------------------- state

def test_state_secret_redacted_in_cli(sb):
    from pi_bridge import cli
    hs.route_install("topsecret")
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.main(["hermes-setup", "state"])
    out = buf.getvalue()
    assert "topsecret" not in out and "redacted" in out


def test_removal_keeps_user_added_platform_keys(sb):
    # We created the platform; the user later adds their own key under it.
    hs.route_install("s")
    from ruamel.yaml import YAML
    yml = YAML()
    doc = yml.load(hs.config_path().read_text())
    doc["platforms"]["webhook"]["my-own-thing"] = 42
    import io
    buf = io.StringIO()
    yml.dump(doc, buf)
    hs.config_path().write_text(buf.getvalue())
    st = hs.load_state()
    st["wake"]["hermes_home"] = str(hs.hermes_home())  # same host, ownership intact
    hs.save_state(st)
    assert hs.route_remove()["route"] == "removed"
    doc = _read_cfg()
    assert doc["platforms"]["webhook"]["my-own-thing"] == 42


def test_missing_host_is_treated_as_public_bind(sb):
    # Hermes binds ALL interfaces when extra.host is absent -> must refuse
    hs.config_path().write_text("""\
platforms:
  webhook:
    enabled: true
    extra:
      port: 8644
      routes: {}
""")
    with pytest.raises(BridgeError):
        hs.route_install("s")
    r = hs.route_install("s", allow_non_loopback=True)
    assert r["route"] == "created_route"


def test_non_mapping_platforms_rejected_cleanly(sb):
    hs.config_path().write_text("platforms: true\n")
    with pytest.raises(BridgeError):
        hs.route_install("s")
    hs.config_path().write_text("platforms:\n  webhook: true\n")
    with pytest.raises(BridgeError):
        hs.route_install("s")


def test_write_preserves_existing_file_mode(sb):
    import os
    p = hs.soul_path()
    p.write_text("soul\n")
    os.chmod(p, 0o644)
    hs.soul_install_file()
    assert stat.S_IMODE(p.stat().st_mode) == 0o644
    hs.config_path().write_text("platforms: {}\n")
    os.chmod(hs.config_path(), 0o664)
    hs.route_install("s")
    assert stat.S_IMODE(hs.config_path().stat().st_mode) == 0o664
    # our fully-owned new files stay 0600
    assert stat.S_IMODE(hs.state_path().stat().st_mode) == 0o600
