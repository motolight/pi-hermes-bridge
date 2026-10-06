#!/usr/bin/env bash
# pi-hermes-bridge installer — checks compatibility, installs CLI + plugin.
# Never installs/updates Hermes, Pi, pi-open-agents or PI WEB.
# Never restarts a running Hermes gateway without asking.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say(){ printf '\n== %s\n' "$*"; }
die(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
ask(){ printf '%s [y/N] ' "$*"; read -r a; [[ "$a" == y || "$a" == Y ]]; }

PYTHON="${PYTHON:-python3}"

say "1/6 prerequisite check"
command -v "$PYTHON" >/dev/null || die "python3 not found (needed for the bridge venv)"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' \
  || die "Python >= 3.10 required, found: $("$PYTHON" -V 2>&1)"

PI_BIN="${PI_BRIDGE_PI_BIN:-$(command -v pi || true)}"
[ -n "$PI_BIN" ] || die "Pi CLI not found. Install the Pi Coding Agent and ensure 'pi' is in PATH, or set PI_BRIDGE_PI_BIN=/path/to/pi and re-run."

say "2/6 Pi compatibility (version + print-mode capability)"
"$PI_BIN" --version || die "'$PI_BIN' is not runnable"
PI_HELP="$("$PI_BIN" --help 2>&1 || true)"
for flag in --print --agent --session-id --session-dir; do
  printf '%s\n' "$PI_HELP" | grep -q -- "$flag" \
    || die "Pi at $PI_BIN lacks $flag — the bridge requires pi print mode with --print/--agent/--session-id/--session-dir$(
         [ "$flag" = --agent ] && printf '%s' ' (note: --agent is provided by the pi-open-agents extension or an equivalent, not by pi core — install/configure it in the pi agent home first, see docs/PI_ORCHESTRATOR_EXAMPLE.md)')"
done
printf 'Pi OK: %s\n' "$("$PI_BIN" --version | head -1)"

HAVE_HERMES=0; command -v hermes >/dev/null && HAVE_HERMES=1
# Same probe the bridge itself uses to decide the launcher (systemd-run --user).
HAVE_SYSTEMD=0
[ -n "${XDG_RUNTIME_DIR:-}" ] && systemctl --user show --property=DefaultTarget >/dev/null 2>&1 && HAVE_SYSTEMD=1
[ "$HAVE_SYSTEMD" = 1 ] || printf 'NOTE: no systemd user session -> jobs run detached but will not survive session logout/reboot.\n'
[ "$HAVE_HERMES" = 1 ] || printf 'NOTE: Hermes CLI not found -> plugin install skipped; bridge CLI still installs (API/CLI only).\n'

say "3/6 python venv + bridge CLI"
[ -d .venv ] || "$PYTHON" -m venv .venv
.venv/bin/pip install -q --upgrade pip >/dev/null
.venv/bin/pip install -q -e .
.venv/bin/pi-bridge --help >/dev/null || die "pi-bridge CLI did not install cleanly"
# Make the CLI reachable from the Hermes gateway process: the pi-worker
# plugin resolves it via $PI_BRIDGE_CLI -> PATH -> ~/.local/bin/pi-bridge.
if [ -w "$HOME/.local/bin" ] || mkdir -p "$HOME/.local/bin" 2>/dev/null; then
  ln -sf "$PWD/.venv/bin/pi-bridge" "$HOME/.local/bin/pi-bridge" \
    && printf 'pi-bridge CLI: %s/.venv/bin/pi-bridge (linked at ~/.local/bin/pi-bridge)\n' "$PWD" \
    || printf 'WARNING: could not link ~/.local/bin/pi-bridge; set PI_BRIDGE_CLI=%s/.venv/bin/pi-bridge for the gateway environment.\n' "$PWD"
else
  printf 'WARNING: cannot create ~/.local/bin; set PI_BRIDGE_CLI=%s/.venv/bin/pi-bridge for the gateway environment.\n' "$PWD"
fi
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) printf 'NOTE: add ~/.local/bin to PATH (also in the environment the Hermes gateway runs in) so the pi-worker plugin finds pi-bridge.\n' ;;
esac

say "4/6 record pi binary (discovery + capability check -> bridge config)"
.venv/bin/pi-bridge install --pi-bin "$PI_BIN" --json || die "pi capability check failed for $PI_BIN"

say "5/6 Hermes plugin pi-worker"
if [ "$HAVE_HERMES" = 1 ]; then
  # Hermes' `plugins install` takes catalog names / Git URLs, not local
  # directories; the supported way to install a user plugin from a checkout
  # is copying it into the Hermes plugin dir.
  mkdir -p ~/.hermes/plugins/pi-worker
  cp -R plugin/. ~/.hermes/plugins/pi-worker/
  rm -rf ~/.hermes/plugins/pi-worker/__pycache__
  printf 'Plugin copied to ~/.hermes/plugins/pi-worker.\n'
  if ask "Enable plugin pi-worker now?"; then
    hermes plugins enable pi-worker
    printf 'Plugin enabled. IMPORTANT: restart the gateway when convenient: hermes gateway restart\n'
  else
    printf 'Skipped. Enable later: hermes plugins enable pi-worker && hermes gateway restart\n'
  fi
fi

say "6/6 self-test"
.venv/bin/python -m pip install -q pytest >/dev/null 2>&1 || true
.venv/bin/python -m pytest tests/ -q 2>&1 | tail -1 || printf 'NOTE: test run needs pytest in venv; run manually: .venv/bin/python -m pytest tests/ -q\n'

say "done"
cat <<'EOF'
Next (optional, recommended):
  * enable the completion wake channel:  docs/WAKE_SETUP.md
  * routing policy snippet for Hermes:   docs/ROUTING_POLICY.md
  * example Pi orchestrator config:      docs/PI_ORCHESTRATOR_EXAMPLE.md
  * troubleshooting:                     docs/TROUBLESHOOTING.md
EOF
