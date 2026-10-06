#!/usr/bin/env bash
# Remove pi-hermes-bridge. Touches nothing that belongs to Hermes/Pi/PI WEB cores.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ask(){ printf '%s [y/N] ' "$*"; read -r a; [[ "$a" == y || "$a" == Y ]]; }

if command -v hermes >/dev/null 2>&1; then
  hermes plugins disable pi-worker 2>/dev/null || true
  if ask "Remove Hermes plugin pi-worker?"; then
    hermes plugins remove pi-worker 2>/dev/null || rm -rf ~/.hermes/plugins/pi-worker
    printf 'Plugin removed. Restart the gateway when convenient: hermes gateway restart\n'
  fi
fi
rm -f "${PI_BRIDGE_BIN:-$HOME/.local/bin/pi-bridge}" 2>/dev/null || true
if ask "Delete bridge state (jobs/results/logs) in \${PI_BRIDGE_HOME:-$HOME/.local/state/pi-bridge}?"; then
  rm -rf "${PI_BRIDGE_HOME:-$HOME/.local/state/pi-bridge}"
fi
if ask "Delete this checkout (incl. .venv)?"; then
  printf 'Run: cd ~ && rm -rf %s\n' "$PWD"
fi
printf 'Done. Hermes, Pi, pi-open-agents and PI WEB were not modified.\n'
