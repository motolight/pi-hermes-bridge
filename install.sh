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
"$PI_BIN" --help 2>&1 | grep -q -- "--print"      || die "Pi at $PI_BIN lacks --print (print mode) — bridge requires Pi CLI print mode"
"$PI_BIN" --help 2>&1 | grep -q -- "--session"    || die "Pi at $PI_BIN lacks --session-id (session continuation) — required for pi_feedback"
printf 'Pi OK: %s\n' "$("$PI_BIN" --version | head -1)"

HAVE_HERMES=0; command -v hermes >/dev/null && HAVE_HERMES=1
HAVE_SYSTEMD=0; systemctl --user is-system-state >/dev/null 2>&1 && HAVE_SYSTEMD=1
[ "$HAVE_SYSTEMD" = 1 ] || printf 'NOTE: no systemd user session -> jobs run detached but will not survive session logout/reboot.\n'
[ "$HAVE_HERMES" = 1 ] || printf 'NOTE: Hermes CLI not found -> plugin install skipped; bridge CLI still installs (API/CLI only).\n'

say "3/6 python venv + bridge CLI"
[ -d .venv ] || "$PYTHON" -m venv .venv
.venv/bin/pip install -q --upgrade pip >/dev/null
.venv/bin/pip install -q -e .
.venv/bin/pi-bridge --help >/dev/null || die "pi-bridge CLI did not install cleanly"
printf 'pi-bridge CLI: %s/.venv/bin/pi-bridge\n' "$PWD"

say "4/6 record pi binary (discovery + capability check -> bridge config)"
.venv/bin/pi-bridge install --pi-bin "$PI_BIN" --json || die "pi capability check failed for $PI_BIN"

say "5/6 Hermes plugin pi-worker"
if [ "$HAVE_HERMES" = 1 ]; then
  hermes plugins install "$PWD/plugin" 2>/dev/null \
    || { mkdir -p ~/.hermes/plugins/pi-worker && cp -r plugin/* ~/.hermes/plugins/pi-worker/; printf 'Plugin copied to ~/.hermes/plugins/pi-worker (manual mode).\n'; }
  if ask "Enable plugin pi-worker now?"; then
    hermes plugins enable pi-worker
    printf 'Plugin enabled. IMPORTANT: restart the gateway when convenient: hermes gateway restart\n'
  else
    printf 'Skipped. Enable later: hermes plugins enable pi-worker && hermes gateway restart\n'
  fi
fi

say "6/6 self-test"
"$PYTHON" -m pip --version >/dev/null 2>&1 || true
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
