#!/usr/bin/env python3
"""Manage the Hermes WebUI "pi-lamp" extension (writer timer + registration).

Subcommands
  status     read-only overview (hashes, units, status.json freshness)
  install    idempotent: writer units + assets sync + registration
  update     redeploy current source (JS/CSS/manifest/writer), verify hashes
  disable    unregister + stop/disable the writer timer (WebUI as before; files kept)
  enable     re-register after disable and re-enable the writer timer
  uninstall  disable + remove installed status.json (state kept)
  backup     snapshot the WebUI state files this extension touches

pi-lamp has NO sidecar and NO proxy consent: the browser only reads the
same-origin static file /extensions/pi-lamp/status.json, refreshed by the
local systemd timer hermes-pi-lamp-writer.timer (10s).  This tool manages NO
secrets.  It merges (never rewrites) the shared WebUI registration files, so
the project-folders extension is unaffected.

Rollback: python3 manage.py disable  → reload the WebUI tab.  Like every other
mutating subcommand it refuses to touch a shared registration file it cannot
parse (fail closed), so in that emergency the fix is manual JSON repair, using
the snapshots in ~/backups.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

EXT_ID = "pi-lamp"
VERSION = "1.1.0"
SRC = Path(os.environ.get("PILAMP_SRC", str(Path(__file__).resolve().parent)))
EXT_ROOT = Path(os.environ.get("PILAMP_EXT_ROOT",
                               str(Path.home() / ".hermes/webui/extensions/pi-lamp")))
WEBUI_STATE_DIR = Path(os.environ.get(
    "PILAMP_WEBUI_STATE_DIR",
    str(Path.home() / ".hermes" / "webui"))).expanduser()
INSTALL_MANIFEST = WEBUI_STATE_DIR / "extension-install-manifest.json"
OVERRIDES = WEBUI_STATE_DIR / "extension-overrides.json"
STATUS_JSON = EXT_ROOT / "status.json"

# Injectable so tests (and non-standard installs) can point every side effect
# at a sandbox.  Defaults are the real host locations.
BACKUP_ROOT = Path(os.environ.get(
    "PI_LAMP_BACKUP_DIR", str(Path.home() / "backups"))).expanduser()
# pi-bridge state the writer reads (same variable writer.py honours).
BRIDGE_HOME = Path(os.environ.get(
    "PI_LAMP_BRIDGE_HOME",
    str(Path.home() / ".local" / "state" / "pi-bridge"))).expanduser()
UNITS_DIR = Path(os.environ.get("PI_LAMP_UNITS_DIR",
                                str(Path.home() / ".config/systemd/user")))
SYSTEMCTL_BIN = shlex.split(os.environ.get("PI_LAMP_SYSTEMCTL", "systemctl"))

SERVICE = "hermes-pi-lamp-writer.service"
TIMER = "hermes-pi-lamp-writer.timer"
WRITER = SRC / "writer.py"
STALL_S = os.environ.get("PI_LAMP_STALL_S", "420")
# The two badge-policy knobs, so they are settable from manage.py's
# environment instead of by hand-editing a unit file that `update` rewrites.
DONE_QUIET_S = os.environ.get("PI_LAMP_DONE_QUIET_S", "10800")
NOTABLE_WINDOW_S = os.environ.get("PI_LAMP_NOTABLE_WINDOW_S", "86400")
PIWEB_CONFIG = os.environ.get("PI_WEB_CONFIG", str(Path.home() / ".config" / "pi-web" / "config.json"))
HTTP_BUDGET_S = os.environ.get("PI_LAMP_HTTP_BUDGET_S", "4")
WRITER_CACHE = os.environ.get("PI_LAMP_CACHE", str(Path.home() / ".local/state/pi-lamp/piweb-cache.json"))

ASSET_FILES = ("manifest.json", "assets/pi-lamp.js", "assets/pi-lamp.css")
KEEP_IN_ROOT = ("status.json",)  # writer-produced, never treated as code
OWNED = ASSET_FILES + KEEP_IN_ROOT

# Shared WebUI state files are tiny.  Anything larger (or of an unexpected
# shape) is treated as corrupt/unexpected and is never rewritten: fail closed
# rather than replace it with a default that drops co-tenant extensions.
MAX_SHARED_STATE_BYTES = 512 * 1024
DEFAULT_MANIFEST = {"version": 1, "installed": {}}
DEFAULT_OVERRIDES = {"version": 1, "disabled_extensions": [], "sidecar_proxy_consents": {}}


def _log(msg: str) -> None:
    print(f"[pi-lamp-manage] {msg}", flush=True)


def _die(msg: str) -> None:
    print(f"[pi-lamp-manage] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


class SharedStateError(RuntimeError):
    """A shared WebUI state file exists but cannot be merged safely."""


def _load_json(path: Path, default: dict) -> dict:
    """Read a shared WebUI state file, failing closed.

    Only a genuinely *absent* file yields `default`.  A file that exists but is
    unreadable, oversized, not valid JSON or not a JSON object raises
    SharedStateError: overwriting it with a default would silently drop other
    extensions' entries (notably `project-folders`).
    """
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        return copy.deepcopy(default)
    except OSError as exc:
        raise SharedStateError(f"cannot read {path}: {exc}") from exc
    if len(raw_bytes) > MAX_SHARED_STATE_BYTES:
        raise SharedStateError(
            f"{path} is {len(raw_bytes)} bytes (limit {MAX_SHARED_STATE_BYTES}); "
            "refusing to rewrite shared WebUI state")
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SharedStateError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SharedStateError(
            f"{path} holds {type(raw).__name__}, not a JSON object; "
            "refusing to rewrite shared WebUI state")
    return raw


def _mergeable(obj: dict, key: str, expected: type, path: Path):
    """Return obj[key], or None when absent (safe to add).

    A present-but-wrongly-typed value cannot be merged without dropping data,
    so it aborts instead of being replaced.
    """
    if key not in obj:
        return None
    val = obj[key]
    if not isinstance(val, expected):
        raise SharedStateError(
            f"{path}: key {key!r} holds {type(val).__name__}, not "
            f"{expected.__name__}; refusing to rewrite shared WebUI state")
    return val


def preflight_shared_state() -> tuple:
    """Read both shared WebUI state files *before* any mutation."""
    return (_load_json(INSTALL_MANIFEST, DEFAULT_MANIFEST),
            _load_json(OVERRIDES, DEFAULT_OVERRIDES))


def _save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def backup(tag: str = "") -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_ROOT / f"webui-pi-lamp-{tag or 'manage'}-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for f in ("extension-install-manifest.json", "extension-overrides.json"):
        src = WEBUI_STATE_DIR / f
        if src.exists():
            shutil.copy2(src, dest / f)
    for u in (SERVICE, TIMER):
        p = UNITS_DIR / u
        if p.exists():
            shutil.copy2(p, dest / u)
    if EXT_ROOT.exists():
        shutil.copytree(EXT_ROOT, dest / "installed-extension", dirs_exist_ok=True)
    _log(f"backup written to {dest}")
    return dest


# ---------------------------------------------------------------- units

def _service_content() -> str:
    return f"""[Unit]
Description=Pi Lamp writer - pi-bridge job status snapshot for the WebUI

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {WRITER}
Environment=PI_BRIDGE_HOME={BRIDGE_HOME}
Environment=PI_LAMP_OUT={STATUS_JSON}
Environment=PI_LAMP_STALL_S={STALL_S}
Environment=PI_LAMP_DONE_QUIET_S={DONE_QUIET_S}
Environment=PI_LAMP_NOTABLE_WINDOW_S={NOTABLE_WINDOW_S}
Environment=PI_LAMP_HTTP_BUDGET_S={HTTP_BUDGET_S}
Environment=PI_LAMP_CACHE={WRITER_CACHE}
Environment=PI_WEB_CONFIG={PIWEB_CONFIG}
Nice=10
NoNewPrivileges=yes
ProtectSystem=full
"""


def _timer_content() -> str:
    return f"""[Unit]
Description=Refresh pi-lamp status.json every 10s

[Timer]
OnBootSec=5
OnUnitActiveSec=10
AccuracySec=2
Unit={SERVICE}

[Install]
WantedBy=timers.target
"""


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = [*SYSTEMCTL_BIN, "--user", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        if not check:
            _log(f"warning: systemctl --user {' '.join(args)} failed: {exc}")
            return subprocess.CompletedProcess(cmd, 127, "", str(exc))
        _die(f"systemd operation failed: {exc}")
    if check and r.returncode != 0:
        _die(f"systemctl --user {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r


def install_units() -> bool:
    UNITS_DIR.mkdir(parents=True, exist_ok=True)
    changed = False
    for name, content in ((SERVICE, _service_content()), (TIMER, _timer_content())):
        p = UNITS_DIR / name
        if not p.exists() or p.read_text() != content:
            p.write_text(content)
            changed = True
            _log(f"wrote {p}")
    if changed:
        _systemctl("daemon-reload")
    _systemctl("enable", TIMER)
    _systemctl("restart", TIMER)
    _log("writer timer enabled+started")
    return changed


def run_writer_once() -> None:
    r = subprocess.run(["/usr/bin/python3", str(WRITER)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        _die(f"writer.py failed: {r.stderr.strip()[:400]}")


# ---------------------------------------------------------------- assets

TEMP_PREFIXES = (".tmp-",)          # writer.py: OUT_PATH.with_name(".tmp-" + pid)
TEMP_SUFFIXES = (".tmp",)           # generic atomic-write leftovers
OWN_TEMP_MARKER = ".pilamp-tmp."    # this tool's own atomic-write temp files


def is_temp_file(name: str) -> bool:
    """True for in-flight atomic-write temp files the sweep must never touch."""
    return (name.startswith(TEMP_PREFIXES) or name.endswith(TEMP_SUFFIXES)
            or OWN_TEMP_MARKER in name)


def stale_installed_files(root: Path, keep: tuple = OWNED) -> list:
    """List installed files under `root` that this deploy does not own.

    Pure: deletes nothing.  Walks without following symlinks so nothing outside
    pi-lamp's own installed dir is ever considered, and skips in-flight temp
    files (writer's `.tmp-*`, ours `*.pilamp-tmp.*`, `*.tmp`).
    """
    found = []
    for dirpath, _dirs, files in os.walk(root, followlinks=False):
        for name in sorted(files):
            p = Path(dirpath) / name
            if is_temp_file(name):
                continue
            if p.is_symlink():  # never follow; leave foreign links alone
                continue
            if p.relative_to(root).as_posix() in keep:
                continue
            found.append(p)
    return found


def cleanup_own_temps(root: Path, max_age_s: int = 3600) -> list:
    """Remove this tool's own crashed atomic-write temp files under `root`."""
    removed = []
    now = time.time()
    for dirpath, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            if OWN_TEMP_MARKER not in name:
                continue
            p = Path(dirpath) / name
            try:
                if now - p.stat().st_mtime >= max_age_s:
                    p.unlink()
                    removed.append(p)
            except OSError:
                pass
    return removed


def _assert_safe_ext_root() -> None:
    root = EXT_ROOT.resolve()
    if not EXT_ROOT.is_absolute() or root in (Path("/"), Path.home()):
        _die(f"refusing to sync assets into unsafe extension root: {EXT_ROOT}")
    for protected in (SRC.resolve(), WEBUI_STATE_DIR.resolve()):
        if root == protected or root in protected.parents:
            _die(f"refusing to sync assets into {root}: it is or contains "
                 f"{protected}, which holds shared state or source")
    # Being *inside* the WebUI state dir is the normal case, but only ever at
    # .../extensions/<our id>: the sync deletes files it does not own, so it
    # must never run in the extensions root, another extension's directory, or
    # any other corner of the shared state tree.
    state_dir = WEBUI_STATE_DIR.resolve()
    if state_dir in root.parents and root != state_dir / "extensions" / EXT_ID:
        _die(f"refusing to sync assets into {root}: inside the shared WebUI "
             f"state tree but not this extension's own directory "
             f"(expected .../extensions/{EXT_ID})")


def sync_assets() -> None:
    for rel in ASSET_FILES:
        if not (SRC / rel).is_file():
            _die(f"source asset missing: {SRC / rel}")
    _assert_safe_ext_root()
    EXT_ROOT.mkdir(parents=True, exist_ok=True)
    for rel in ASSET_FILES:
        src, dst = SRC / rel, EXT_ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + f"{OWN_TEMP_MARKER}{os.getpid()}")
        try:
            shutil.copyfile(src, tmp)
            os.chmod(tmp, 0o644)
            os.replace(tmp, dst)
        except BaseException:
            tmp.unlink(missing_ok=True)  # never leave our own temp behind
            raise
    for p in cleanup_own_temps(EXT_ROOT):
        _log(f"removed stale pi-lamp temp file: {p.name}")
    for p in sorted(stale_installed_files(EXT_ROOT)):
        p.unlink()
        _log(f"removed stale installed file: {p.relative_to(EXT_ROOT).as_posix()}")
    for rel in ASSET_FILES:
        if sha256(SRC / rel) != sha256(EXT_ROOT / rel):
            _die(f"hash mismatch after deploy: {rel}")
    _log("assets synced + hash-verified")


def verify_assets() -> bool:
    for rel in ASSET_FILES:
        s, d = SRC / rel, EXT_ROOT / rel
        if not s.is_file() or not d.is_file() or sha256(s) != sha256(d):
            return False
    return True


# ---------------------------------------------------------------- registration

def register_extension() -> None:
    # Both shared files are read (and validated) before either is written, so a
    # failure can never leave a half-updated registration behind.
    manifest, overrides = preflight_shared_state()
    installed = _mergeable(manifest, "installed", dict, INSTALL_MANIFEST)
    if installed is None:
        installed = {}
    installed[EXT_ID] = {
        "version": VERSION,
        "files": list(ASSET_FILES),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    manifest["installed"] = installed

    disabled = _mergeable(overrides, "disabled_extensions", list, OVERRIDES)
    if disabled is None:
        disabled = []
    overrides["disabled_extensions"] = [d for d in disabled if d != EXT_ID]
    consents = _mergeable(overrides, "sidecar_proxy_consents", dict, OVERRIDES)
    if consents is None:
        consents = {}
    consents.pop(EXT_ID, None)  # pi-lamp has no sidecar; co-tenant consents kept
    overrides["sidecar_proxy_consents"] = consents
    overrides.setdefault("version", 1)
    _save_json(INSTALL_MANIFEST, manifest)
    _save_json(OVERRIDES, overrides)
    _log(f"registered {EXT_ID} v{VERSION} (other extensions untouched)")


def unregister_extension() -> None:
    manifest, overrides = preflight_shared_state()  # read both before writing either
    installed = _mergeable(manifest, "installed", dict, INSTALL_MANIFEST)
    if installed is None:
        installed = {}
    disabled = _mergeable(overrides, "disabled_extensions", list, OVERRIDES)
    if disabled is None:
        disabled = []
    if EXT_ID in installed:
        del installed[EXT_ID]
        manifest["installed"] = installed
        _save_json(INSTALL_MANIFEST, manifest)
        _log(f"removed {EXT_ID} from {INSTALL_MANIFEST.name}")
    if EXT_ID not in disabled:
        overrides["disabled_extensions"] = list(disabled) + [EXT_ID]
        # sidecar_proxy_consents and every other co-tenant key pass through
        # untouched: we only ever add pi-lamp to disabled_extensions.
        _save_json(OVERRIDES, overrides)
    _log(f"{EXT_ID} disabled")


# ---------------------------------------------------------------- writer units

def writer_unit_ops(action: str) -> list:
    """Pure: `systemctl --user` argument tuples for a writer-unit lifecycle action.

    The writer service is a oneshot unit with no [Install] section (it is
    activated by the timer), so it is not enableable and `systemctl disable`
    on it is an error on some systemd versions: only the timer is enabled and
    disabled, the service is merely stopped. Both must stop, otherwise the 10s
    timer keeps firing after the extension is unregistered.
    """
    if action == "enable":
        return [("enable", TIMER), ("start", TIMER)]
    if action in ("disable", "uninstall"):
        return [("disable", "--now", TIMER), ("stop", SERVICE)]
    raise ValueError(f"unknown writer unit action: {action!r}")


def writer_timer_active() -> bool:
    """Is the refresh timer still firing? (Never fatal.)"""
    return _systemctl("is-active", "--quiet", TIMER, check=False).returncode == 0


def apply_writer_unit_ops(action: str) -> None:
    """Run writer_unit_ops(action); idempotent, never fatal (no services restarted)."""
    for args in writer_unit_ops(action):
        r = _systemctl(*args, check=False)
        if r.returncode != 0:
            _log(f"warning: systemctl --user {' '.join(args)}: "
                 f"{(r.stderr or r.stdout).strip()[:200]}")
    _log(f"writer units: {action}")


def remove_status_file(attempts: int = 2, wait_s: float = 1.2) -> bool:
    """Delete the writer-produced status.json from the installed extension dir.

    It carries job ids, error text and LAN URLs and stays fetchable through the
    WebUI /extensions static route even once unregistered, so uninstall removes
    it.  A writer tick that was already in flight can recreate it once, hence
    the short second pass.  Idempotent: returns True only if something was
    actually deleted.
    """
    removed = False
    for i in range(max(1, attempts)):
        if i and wait_s:
            time.sleep(wait_s)
        if not STATUS_JSON.exists():
            break
        try:
            STATUS_JSON.unlink()
            removed = True
            _log(f"removed {STATUS_JSON}")
        except OSError as exc:
            _log(f"warning: could not remove {STATUS_JSON}: {exc}")
            break
    if removed and STATUS_JSON.exists():
        _log(f"WARNING: {STATUS_JSON} came back — stop the writer with "
             f"systemctl --user disable --now {TIMER}")
    return removed


# ---------------------------------------------------------------- commands

def cmd_status() -> None:
    ok_assets = verify_assets()
    _log(f"installed dir: {EXT_ROOT} ({'assets match source' if ok_assets else 'DRIFT — run update'})")
    r = _systemctl("is-active", TIMER, check=False)
    _log(f"timer: {r.stdout.strip() or 'inactive'}")
    if STATUS_JSON.exists():
        age = time.time() - STATUS_JSON.stat().st_mtime
        _log(f"status.json: {STATUS_JSON} age {age:.0f}s "
             f"({'FRESH' if age < 90 else 'STALE — writer not running?'})")
    else:
        _log("status.json: MISSING (run install)")
    for other in ("project-folders",):
        try:
            m = _load_json(INSTALL_MANIFEST, DEFAULT_MANIFEST).get("installed", {})
        except SharedStateError as exc:
            _log(f"co-tenant {other}: UNKNOWN ({exc})")
            continue
        m = m if isinstance(m, dict) else {}
        _log(f"co-tenant {other}: {'registered' if other in m else 'NOT registered'}")


def cmd_install() -> None:
    preflight_shared_state()  # abort before touching anything if unreadable
    backup("pre-install")
    sync_assets()
    install_units()
    run_writer_once()
    register_extension()
    _log("installed. Reload the WebUI tab; badges appear within ~15s of a job starting.")


def cmd_update() -> None:
    preflight_shared_state()  # abort before touching anything if unreadable
    backup("pre-update")
    sync_assets()
    install_units()
    register_extension()


def cmd_enable() -> None:
    preflight_shared_state()
    backup("pre-enable")
    register_extension()
    apply_writer_unit_ops("enable")


def cmd_disable() -> None:
    preflight_shared_state()
    backup("pre-disable")
    unregister_extension()
    apply_writer_unit_ops("disable")
    if writer_timer_active():
        _log(f"WARNING: {TIMER} is still active (systemctl unavailable?) — the "
             f"writer keeps refreshing status.json until it is stopped")
    _log("unregistered; writer timer stopped+disabled (files kept, reload the tab)")


def cmd_uninstall() -> None:
    preflight_shared_state()
    backup("pre-uninstall")
    unregister_extension()
    apply_writer_unit_ops("uninstall")
    remove_status_file()
    if writer_timer_active():
        _log(f"WARNING: {TIMER} is still active — status.json will be recreated "
             f"and stays fetchable via /extensions/{EXT_ID}/status.json")
    _log("writer units stopped+disabled; status.json removed; files and state kept")


CMDS = {"status": cmd_status, "install": cmd_install, "update": cmd_update,
        "enable": cmd_enable, "disable": cmd_disable, "uninstall": cmd_uninstall,
        "backup": lambda: backup("manual")}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in CMDS:
        print(__doc__)
        sys.exit(1)
    try:
        CMDS[sys.argv[1]]()
    except SharedStateError as exc:
        _die(f"{exc} — no changes made")
