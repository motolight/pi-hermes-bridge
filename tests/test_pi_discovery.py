"""Regression: pi binary discovery must work when PATH lacks the pi directory.

Priority chain: explicit arg > PI_BRIDGE_PI_BIN > bridge config.json (absolute,
written by `pi-bridge install`) > shutil.which("pi") > clear error.
"""
import json
import stat

import pytest

from pi_bridge import bridge
from pi_bridge.state import bridge_home


def _fake_pi(tmp_path, name="pi-fake"):
    p = tmp_path / name
    p.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo 0.84.4; exit 0; fi\n'
        'if [ "$1" = "--help" ]; then echo "--print --agent --session-id --session-dir"; exit 0; fi\n'
        "exit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_config_pi_bin_used_when_path_lacks_pi(tmp_path, monkeypatch):
    pi = _fake_pi(tmp_path)
    # config carries the absolute path (as `install` would write it)
    monkeypatch.setattr(bridge, "bridge_config",
                        lambda: {"pi_bin": pi})
    # PATH deliberately excludes anything findable
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    monkeypatch.delenv("PI_BRIDGE_PI_BIN", raising=False)
    assert bridge.resolve_pi_bin(None) == str(__import__("pathlib").Path(pi).resolve())


def test_env_overrides_config(tmp_path, monkeypatch):
    env_pi = _fake_pi(tmp_path, "pi-env")
    cfg_pi = _fake_pi(tmp_path, "pi-cfg")
    monkeypatch.setattr(bridge, "bridge_config", lambda: {"pi_bin": cfg_pi})
    monkeypatch.setenv("PI_BRIDGE_PI_BIN", env_pi)
    assert bridge.resolve_pi_bin(None).endswith("pi-env")


def test_arg_overrides_everything(tmp_path, monkeypatch):
    arg_pi = _fake_pi(tmp_path, "pi-arg")
    monkeypatch.setattr(bridge, "bridge_config", lambda: {"pi_bin": _fake_pi(tmp_path, "pi-cfg")})
    monkeypatch.setenv("PI_BRIDGE_PI_BIN", _fake_pi(tmp_path, "pi-env"))
    assert bridge.resolve_pi_bin(arg_pi).endswith("pi-arg")


def test_clear_error_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "bridge_config", lambda: {})
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    monkeypatch.delenv("PI_BRIDGE_PI_BIN", raising=False)
    with pytest.raises(bridge.BridgeError) as e:
        bridge.resolve_pi_bin(None)
    msg = str(e.value)
    assert "not found" in msg and "PI_BRIDGE_PI_BIN" in msg and "pi-bridge install" in msg


def test_stale_config_path_falls_through_to_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "bridge_config", lambda: {"pi_bin": str(tmp_path / "gone")})
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    monkeypatch.delenv("PI_BRIDGE_PI_BIN", raising=False)
    with pytest.raises(bridge.BridgeError):
        bridge.resolve_pi_bin(None)


def test_capability_check_rejects_broken_binary(tmp_path):
    p = tmp_path / "bad-pi"
    p.write_text("#!/bin/sh\nexit 3\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(bridge.BridgeError):
        bridge.check_pi_capability(str(p))
