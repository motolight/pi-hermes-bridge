"""Registration merge safety: pi-lamp must never clobber other extensions'
entries in the shared WebUI registration files (project-folders co-tenant).

Also covers the hardening rules around those shared files: a shared file that
exists but cannot be read/parsed must abort the whole operation with no writes,
and every mutation path must back both files up first.
"""
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

MANIFEST = "extension-install-manifest.json"
OVERRIDES = "extension-overrides.json"
CO_SENTINEL = "http://127.0.0.1:17872"


def _seed_shared(state: Path) -> tuple:
    """Co-tenant project-folders registered, enabled, with a sidecar consent."""
    manifest = state / MANIFEST
    overrides = state / OVERRIDES
    manifest.write_text(json.dumps({"version": 1, "installed": {
        "project-folders": {"version": "1.2.0", "files": ["manifest.json"]},
    }}), encoding="utf-8")
    overrides.write_text(json.dumps({
        "version": 1,
        "disabled_extensions": ["some-other-ext"],
        "sidecar_proxy_consents": {"project-folders": CO_SENTINEL},
        "theme": "midnight",  # an unrelated co-tenant key we must pass through
    }), encoding="utf-8")
    return manifest, overrides


def _fake_systemctl(tmp_path: Path, rc: int = 0) -> tuple:
    """Recorder standing in for systemctl, so no real unit is ever touched."""
    log = tmp_path / "systemctl.log"
    script = tmp_path / "fake-systemctl.sh"
    script.write_text(f"#!/bin/sh\necho \"$*\" >> \"{log}\"\nexit {rc}\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script), log


def _load_manage(tmp_path: Path, monkeypatch, systemctl: str = "echo"):
    """Import manage.py with every side effect redirected into tmp_path.

    manage.py resolves paths at import time, so the env has to be in place
    before a fresh import.  monkeypatch.setenv restores it afterwards, so
    nothing leaks between tests.
    """
    state = tmp_path / "webui-state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("PILAMP_WEBUI_STATE_DIR", str(state))
    monkeypatch.setenv("PILAMP_EXT_ROOT", str(tmp_path / "ext"))
    monkeypatch.setenv("PI_LAMP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("PI_LAMP_UNITS_DIR", str(tmp_path / "units"))
    monkeypatch.setenv("PI_LAMP_SYSTEMCTL", systemctl)
    for mod in ("manage",):
        sys.modules.pop(mod, None)
    return importlib.import_module("manage"), state


# ------------------------------------------------------- existing behaviour

def test_register_merges_and_preserves_project_folders(tmp_path, monkeypatch):
    m, state = _load_manage(tmp_path, monkeypatch)
    manifest, overrides = _seed_shared(state)

    m.register_extension()
    data = json.loads(manifest.read_text())
    assert "project-folders" in data["installed"] and "pi-lamp" in data["installed"]
    ov = json.loads(overrides.read_text())
    assert ov["sidecar_proxy_consents"]["project-folders"] == CO_SENTINEL

    m.unregister_extension()
    data = json.loads(manifest.read_text())
    assert "pi-lamp" not in data["installed"] and "project-folders" in data["installed"]
    ov = json.loads(overrides.read_text())
    assert ov["disabled_extensions"] == ["some-other-ext", "pi-lamp"]


# ------------------------------------------- (b) merge keeps co-tenant data

def test_register_preserves_cotenant_entry_and_consent(tmp_path, monkeypatch):
    m, state = _load_manage(tmp_path, monkeypatch)
    manifest, overrides = _seed_shared(state)

    m.register_extension()

    man = json.loads(manifest.read_text())
    assert man["installed"]["project-folders"] == {"version": "1.2.0", "files": ["manifest.json"]}
    assert man["installed"]["pi-lamp"]["files"] == list(m.ASSET_FILES)
    assert man["version"] == 1

    ov = json.loads(overrides.read_text())
    # exactly the co-tenant consent, plus pi-lamp removed from the disabled list
    assert ov["sidecar_proxy_consents"] == {"project-folders": CO_SENTINEL}
    assert ov["disabled_extensions"] == ["some-other-ext"]
    assert ov["theme"] == "midnight"


# --------------------------------------- (c) unregister touches only pi-lamp

def test_unregister_removes_only_pilamp_and_keeps_consent(tmp_path, monkeypatch):
    m, state = _load_manage(tmp_path, monkeypatch)
    manifest, overrides = _seed_shared(state)
    m.register_extension()

    m.unregister_extension()

    man = json.loads(manifest.read_text())
    assert set(man["installed"]) == {"project-folders"}
    ov = json.loads(overrides.read_text())
    assert ov["sidecar_proxy_consents"] == {"project-folders": CO_SENTINEL}
    assert ov["disabled_extensions"] == ["some-other-ext", "pi-lamp"]
    assert ov["theme"] == "midnight"

    # idempotent: a second unregister changes nothing
    same = overrides.read_bytes()
    m.unregister_extension()
    assert overrides.read_bytes() == same


# ------------------------------------------ (a) fail closed on bad shared file

@pytest.mark.parametrize("name", [MANIFEST, OVERRIDES])
def test_unparseable_shared_file_aborts_without_writing(tmp_path, monkeypatch, name):
    m, state = _load_manage(tmp_path, monkeypatch)
    bad = state / name
    other = state / (OVERRIDES if name == MANIFEST else MANIFEST)
    bad.write_text('{"version": 1, "installed": {"project-folders": ', encoding="utf-8")
    before = bad.read_bytes()

    for op in (m.preflight_shared_state, m.register_extension, m.unregister_extension):
        with pytest.raises(m.SharedStateError):
            op()

    assert bad.read_bytes() == before, "corrupt shared file must be left byte-identical"
    assert not other.exists(), "the other shared file must not be created"
    assert not (tmp_path / "ext").exists()


@pytest.mark.parametrize("payload", [
    "",                                    # empty file
    "not json at all",                     # garbage
    "[1, 2, 3]",                           # valid JSON, wrong shape
    json.dumps({"pad": "x" * (600 * 1024)}),  # absurdly oversized
])
@pytest.mark.parametrize("name", [MANIFEST, OVERRIDES])
def test_unusable_shared_file_payloads_abort(tmp_path, monkeypatch, name, payload):
    m, state = _load_manage(tmp_path, monkeypatch)
    bad = state / name
    bad.write_text(payload, encoding="utf-8")
    before = bad.read_bytes()
    with pytest.raises(m.SharedStateError):
        m.register_extension()
    assert bad.read_bytes() == before


@pytest.mark.parametrize("payload", [
    json.dumps({"version": 1, "installed": "project-folders"}),          # wrong type
    json.dumps({"version": 1, "installed": {"x": 1}, "extra": [1]}),     # ok, unrelated
])
def test_wrong_typed_installed_key_aborts(tmp_path, monkeypatch, payload):
    """A present-but-unmergeable key must abort, never be replaced by {}."""
    m, state = _load_manage(tmp_path, monkeypatch)
    manifest = state / MANIFEST
    manifest.write_text(payload, encoding="utf-8")
    before = manifest.read_bytes()
    if "extra" in payload:  # unrelated extra keys are fine
        m.register_extension()
        assert json.loads(manifest.read_text())["extra"] == [1]
    else:
        with pytest.raises(m.SharedStateError):
            m.register_extension()
        assert manifest.read_bytes() == before


def test_consent_key_with_wrong_type_aborts(tmp_path, monkeypatch):
    """The project-folders sidecar consent must never be silently dropped."""
    m, state = _load_manage(tmp_path, monkeypatch)
    _seed_shared(state)
    overrides = state / OVERRIDES
    overrides.write_text(json.dumps({"version": 1, "disabled_extensions": [],
                                     "sidecar_proxy_consents": ["oops"]}), encoding="utf-8")
    before = overrides.read_bytes()
    with pytest.raises(m.SharedStateError):
        m.register_extension()
    assert overrides.read_bytes() == before


def test_unreadable_shared_file_aborts(tmp_path, monkeypatch):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    m, state = _load_manage(tmp_path, monkeypatch)
    manifest, overrides = _seed_shared(state)
    overrides.chmod(0o000)
    try:
        with pytest.raises(m.SharedStateError):
            m.register_extension()
        assert json.loads(manifest.read_text())["installed"].get("pi-lamp") is None
    finally:
        overrides.chmod(0o600)


def _stub_heavy_side_effects(m, monkeypatch):
    """Keep install/update off the real assets, systemd units and writer."""
    monkeypatch.setattr(m, "sync_assets", lambda: None)
    monkeypatch.setattr(m, "install_units", lambda: False)
    monkeypatch.setattr(m, "run_writer_once", lambda: None)


@pytest.mark.parametrize("cmd", ["cmd_install", "cmd_update"])
def test_install_update_abort_before_touching_anything(tmp_path, monkeypatch, cmd):
    m, state = _load_manage(tmp_path, monkeypatch)
    manifest = state / MANIFEST
    manifest.write_text("{broken", encoding="utf-8")
    before = manifest.read_bytes()
    calls = []
    for name in ("sync_assets", "install_units", "run_writer_once",
                 "register_extension", "backup"):
        monkeypatch.setattr(m, name, lambda _n=name: calls.append(_n))
    with pytest.raises(m.SharedStateError):
        getattr(m, cmd)()
    assert calls == [], f"{cmd} mutated something before the shared-state check"
    assert manifest.read_bytes() == before


# --------------------------------------------- (d) backup before every mutation

@pytest.mark.parametrize("cmd", ["cmd_install", "cmd_update", "cmd_enable",
                                  "cmd_disable", "cmd_uninstall"])
def test_backup_written_before_first_mutation(tmp_path, monkeypatch, cmd):
    m, state = _load_manage(tmp_path, monkeypatch)
    manifest, overrides = _seed_shared(state)
    pre = {p.name: p.read_bytes() for p in (manifest, overrides)}
    backup_root = tmp_path / "backups"

    if cmd in ("cmd_install", "cmd_update"):
        _stub_heavy_side_effects(m, monkeypatch)

    seen = {}
    real_save = m._save_json

    def spy(path, obj):
        seen.setdefault("backups", sorted(p.name for p in backup_root.iterdir())
                        if backup_root.is_dir() else None)
        real_save(path, obj)

    monkeypatch.setattr(m, "_save_json", spy)
    getattr(m, cmd)()

    assert seen["backups"], f"{cmd} wrote shared state before taking a backup"
    snap = backup_root / seen["backups"][0]
    for name, blob in pre.items():
        assert (snap / name).read_bytes() == blob, f"{name} not backed up pre-mutation"
    assert any(p.read_bytes() != pre[p.name] for p in (manifest, overrides)), \
        f"{cmd} made no change at all; test proves nothing"


@pytest.mark.parametrize("cmd", ["cmd_enable", "cmd_disable", "cmd_uninstall"])
def test_mutation_paths_take_backups(tmp_path, monkeypatch, cmd):
    m, state = _load_manage(tmp_path, monkeypatch)
    _seed_shared(state)
    getattr(m, cmd)()
    snaps = [d for d in (tmp_path / "backups").iterdir() if d.is_dir()]
    assert len(snaps) == 1
    assert {p.name for p in snaps[0].iterdir()} == {MANIFEST, OVERRIDES}


# -------------------------------------------------- writer unit lifecycle

def test_writer_unit_ops_are_pure(tmp_path, monkeypatch):
    m, _state = _load_manage(tmp_path, monkeypatch)
    assert m.writer_unit_ops("enable") == [("enable", m.TIMER), ("start", m.TIMER)]
    for action in ("disable", "uninstall"):
        ops = m.writer_unit_ops(action)
        assert ("disable", "--now", m.TIMER) in ops
        # the oneshot service has no [Install] section: it is stopped, never
        # "disable"d (which would be a systemctl error on some versions)
        assert ("stop", m.SERVICE) in ops
        assert not any("restart" in arg for op in ops for arg in op)
        assert ("disable", "--now", m.SERVICE) not in ops
    with pytest.raises(ValueError):
        m.writer_unit_ops("status")


def _systemctl_lines(log: Path) -> list:
    return [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_disable_stops_and_disables_writer_units(tmp_path, monkeypatch):
    systemctl, log = _fake_systemctl(tmp_path)
    m, state = _load_manage(tmp_path, monkeypatch, systemctl=systemctl)
    _seed_shared(state)

    m.cmd_disable()
    lines = _systemctl_lines(log)
    assert f"--user disable --now {m.TIMER}" in lines
    # the oneshot service has no [Install] section: stopped, never "disable"d
    assert f"--user stop {m.SERVICE}" in lines
    assert f"--user disable --now {m.SERVICE}" not in lines
    assert not any("start" in ln or "restart" in ln for ln in lines)


def test_enable_reenables_writer_timer(tmp_path, monkeypatch):
    systemctl, log = _fake_systemctl(tmp_path)
    m, state = _load_manage(tmp_path, monkeypatch, systemctl=systemctl)
    _seed_shared(state)

    m.cmd_enable()
    lines = _systemctl_lines(log)
    assert f"--user enable {m.TIMER}" in lines
    assert f"--user start {m.TIMER}" in lines


def test_lifecycle_ops_survive_systemctl_being_unavailable(tmp_path, monkeypatch):
    """disable must stay idempotent even if systemd cannot be reached."""
    systemctl, _log = _fake_systemctl(tmp_path, rc=1)
    m, state = _load_manage(tmp_path, monkeypatch, systemctl=systemctl)
    _seed_shared(state)
    m.cmd_disable()  # must not raise
    ov = json.loads((state / OVERRIDES).read_text())
    assert ov["disabled_extensions"] == ["some-other-ext", "pi-lamp"]


def test_lifecycle_ops_survive_missing_systemctl(tmp_path, monkeypatch):
    m, state = _load_manage(tmp_path, monkeypatch,
                            systemctl=str(tmp_path / "no-such-systemctl"))
    _seed_shared(state)
    m.cmd_disable()  # must not raise
    assert json.loads((state / OVERRIDES).read_text())["disabled_extensions"] == [
        "some-other-ext", "pi-lamp"]
    m.cmd_enable()
    assert "pi-lamp" in json.loads((state / MANIFEST).read_text())["installed"]
    assert json.loads((state / OVERRIDES).read_text())["disabled_extensions"] == [
        "some-other-ext"]


def test_uninstall_removes_status_json(tmp_path, monkeypatch):
    systemctl, log = _fake_systemctl(tmp_path)
    m, state = _load_manage(tmp_path, monkeypatch, systemctl=systemctl)
    _seed_shared(state)
    ext = Path(m.EXT_ROOT)
    (ext / "assets").mkdir(parents=True, exist_ok=True)
    for rel in m.ASSET_FILES:
        (ext / rel).write_text("installed", encoding="utf-8")
    (ext / "status.json").write_text(json.dumps({"jobs": ["secret-id"],
                                                 "url": "http://192.0.2.1:8787"}))

    m.cmd_uninstall()
    assert not (ext / "status.json").exists()
    assert m.remove_status_file() is False  # idempotent
    assert f"--user disable --now {m.TIMER}" in _systemctl_lines(log)
    assert f"--user stop {m.SERVICE}" in _systemctl_lines(log)
    assert (ext / "manifest.json").exists(), "uninstall keeps installed files"


# --------------------------------------------------------- asset sweep scope

def test_sweep_spares_writer_and_own_temp_files(tmp_path, monkeypatch):
    m, state = _load_manage(tmp_path, monkeypatch)
    ext = Path(m.EXT_ROOT)
    (ext / "assets").mkdir(parents=True)
    (ext / "status.json").write_text("{}", encoding="utf-8")
    writer_tmp = ext / ".tmp-999988"          # writer.py in-flight temp
    writer_tmp.write_text("in-flight", encoding="utf-8")
    generic = ext / "status.json.tmp"          # generic atomic-write leftover
    generic.write_text("in-flight", encoding="utf-8")
    stale = ext / "legacy-asset.js"
    stale.write_text("stale", encoding="utf-8")

    m.sync_assets()

    assert writer_tmp.exists() and generic.exists(), "sweep deleted an in-flight temp file"
    assert not stale.exists()
    assert (ext / "status.json").read_text() == "{}"
    assert m.verify_assets()
    assert not list(ext.rglob("*pilamp-tmp*")), "own temp files not cleaned up"


def test_is_temp_file_patterns():
    import manage as m
    assert m.is_temp_file(".tmp-12345")
    assert m.is_temp_file("pi-lamp.js.pilamp-tmp.12345")
    assert m.is_temp_file("status.json.tmp")
    assert not m.is_temp_file("status.json")
    assert not m.is_temp_file("pi-lamp.js")


def test_cleanup_own_temps_only_touches_our_own(tmp_path, monkeypatch):
    m, _state = _load_manage(tmp_path, monkeypatch)
    ext = Path(m.EXT_ROOT)
    ext.mkdir(parents=True)
    own = ext / "pi-lamp.js.pilamp-tmp.4242"
    own.write_text("x", encoding="utf-8")
    writer = ext / ".tmp-4242"
    writer.write_text("x", encoding="utf-8")
    for p in (own, writer):
        os.utime(p, (0, 0))

    removed = m.cleanup_own_temps(ext)
    assert removed == [own]
    assert writer.exists()


def test_sweep_refuses_extension_root_that_shares_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PILAMP_WEBUI_STATE_DIR", str(tmp_path / "webui-state"))
    monkeypatch.setenv("PILAMP_EXT_ROOT", str(tmp_path / "webui-state" / "nested"))
    sys.modules.pop("manage", None)
    m = importlib.import_module("manage")
    with pytest.raises(SystemExit):
        m.sync_assets()


def test_sweep_allows_the_real_deploy_path(tmp_path, monkeypatch):
    """.../extensions/pi-lamp under the shared state dir IS the production
    layout and must sync; a foreign dir in the same tree must not."""
    state = tmp_path / "webui-state"
    state.mkdir()
    monkeypatch.setenv("PILAMP_WEBUI_STATE_DIR", str(state))
    monkeypatch.setenv("PILAMP_EXT_ROOT", str(state / "extensions" / "pi-lamp"))
    monkeypatch.setenv("PI_LAMP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("PI_LAMP_UNITS_DIR", str(tmp_path / "units"))
    monkeypatch.setenv("PI_LAMP_SYSTEMCTL", "echo")
    sys.modules.pop("manage", None)
    m = importlib.import_module("manage")

    (state / "extensions" / "project-folders").mkdir(parents=True)
    co = state / "extensions" / "project-folders" / "assets"
    co.mkdir()
    (co / "project-folders.js").write_text("// keep me", encoding="utf-8")
    m.EXT_ROOT.mkdir(parents=True)
    (m.EXT_ROOT / "status.json").write_text("{}", encoding="utf-8")

    m.sync_assets()
    assert (m.EXT_ROOT / "assets" / "pi-lamp.js").read_text() == \
        (Path(m.SRC) / "assets" / "pi-lamp.js").read_text()
    assert (m.EXT_ROOT / "status.json").exists()          # writer output survives
    assert (co / "project-folders.js").read_text() == "// keep me"  # co-tenant untouched

    # a foreign directory in the same tree is refused
    monkeypatch.setenv("PILAMP_EXT_ROOT", str(state / "extensions" / "project-folders"))
    sys.modules.pop("manage", None)
    other = importlib.import_module("manage")
    with pytest.raises(SystemExit):
        other.sync_assets()
    assert (co / "project-folders.js").read_text() == "// keep me"
