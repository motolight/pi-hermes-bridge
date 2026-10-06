#!/usr/bin/env bash
# Remove pi-hermes-bridge v0.2 integration. Touches ONLY what the installer
# owns: the pi-worker plugin, the CLI symlink, the managed SOUL.md routing
# block, the pi-routing-policy skill (if we installed it), the bridge-owned
# webhook route, wake.json and the ownership state.
# It does NOT restore whole files from backups, and does NOT remove
# pi-open-agents, Hermes, Pi or PI WEB. Foreign routes / SOUL text / wake
# settings survive.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ask(){
  if [ "${PI_BRIDGE_YES:-0}" = 1 ]; then printf '%s [y/N] y (auto)\n' "$*"; return 0; fi
  printf '%s [y/N] ' "$*"; local a; read -r a || a=""
  [[ "$a" == y || "$a" == Y ]]
}

BRIDGE_PY="$PWD/.venv/bin/python"
[ -x "$BRIDGE_PY" ] || BRIDGE_PY="$(command -v python3 || true)"
FAILED=0
hs(){ "$BRIDGE_PY" -m pi_bridge hermes-setup "$@"; }
jget(){ "$BRIDGE_PY" -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get(sys.argv[1], ""))
except Exception:
    print("")' "$1"; }

HAS_STATE=0
if [ -n "$BRIDGE_PY" ] && [ -n "${PI_BRIDGE_HOME:-}" ] && [ -f "$PI_BRIDGE_HOME/hermes_setup.json" ]; then
  HAS_STATE=1
elif [ -n "$BRIDGE_PY" ] && [ -f "$HOME/.local/state/pi-bridge/hermes_setup.json" ]; then
  HAS_STATE=1
fi

# --- Hermes-side owned changes ---------------------------------------------
# soul/skill removal is marker/sentinel-based and safe even without state;
# route/wake removal is gated by the ownership state itself.
if [ -n "$BRIDGE_PY" ]; then
  printf 'SOUL.md managed routing block: %s\n' "$(hs soul-remove)"
  printf 'pi-routing-policy skill: %s\n' "$(hs skill-remove)"
  R=$(hs route-remove --json || true)
  case "$(printf '%s' "$R" | jget route)" in
    removed)      printf 'webhook route pi-bridge-complete: removed (other routes untouched)\n' ;;
    kept_foreign) printf 'webhook route pi-bridge-complete: NOT ours — left untouched\n' ;;
    absent)       printf 'webhook route pi-bridge-complete: not present\n' ;;
    *)            printf 'ERROR: could not evaluate/remove the webhook route — it may STILL be present in ~/.hermes/config.yaml with granted toolsets.\n'
                  printf '       Remove the route pi-bridge-complete manually, or run uninstall from the checkout venv. Output was: %s\n' "${R:-no output}"
                  FAILED=1 ;;
  esac
  R=$(hs wake-disable --json || true)
  case "$(printf '%s' "$R" | jget wake_json)" in
    removed)       printf 'wake.json: removed\n' ;;
    kept_existing) printf 'wake.json: not owned by the installer — left in place\n' ;;
    absent)        printf 'wake.json: not present\n' ;;
    *)             printf 'ERROR: could not disable/remove wake.json; check ${PI_BRIDGE_HOME:-~/.local/state/pi-bridge}/wake.json manually.\n'
                   FAILED=1 ;;
  esac
  if [ "$HAS_STATE" != 1 ]; then
    printf 'NOTE: no installer ownership state found under \${PI_BRIDGE_HOME:-~/.local/state/pi-bridge}.\n'
    printf '      If you installed with PI_BRIDGE_HOME set elsewhere, re-run: PI_BRIDGE_HOME=<that path> ./uninstall.sh\n'
  fi
else
  echo 'WARNING: no python available — could not evaluate the managed SOUL block / webhook route.' >&2
  echo '         Re-run uninstall.sh from the checkout, or remove by hand: the SOUL.md' >&2
  echo '         block between pi-hermes-bridge markers, platforms.webhook.routes.pi-bridge-complete, wake.json.' >&2
fi

# --- plugin ------------------------------------------------------------------
if command -v hermes >/dev/null 2>&1; then
  hermes plugins disable pi-worker >/dev/null 2>&1 || true
  OWNED=0
  if [ "$HAS_STATE" = 1 ]; then
    OWNED=$(printf '%s' "$(hs state)" | jget plugin_installed)
  fi
  if [ "$OWNED" = "True" ] || [ "$OWNED" = "true" ]; then
    if [ -d "$HOME/.hermes/plugins/pi-worker" ] || hermes plugins list 2>/dev/null | grep -q pi-worker; then
      hermes plugins remove pi-worker >/dev/null 2>&1 || rm -rf "$HOME/.hermes/plugins/pi-worker"
      printf 'Plugin pi-worker removed.\n'
      printf 'Apply now: hermes gateway restart\n'
    else
      printf 'Plugin pi-worker is not installed.\n'
    fi
  else
    printf 'The pi-worker plugin is not recorded as installer-owned (v0.1 or manual install).\n'
    if ask "Remove the Hermes plugin pi-worker anyway?"; then
      hermes plugins remove pi-worker >/dev/null 2>&1 || rm -rf "$HOME/.hermes/plugins/pi-worker"
      printf 'Plugin pi-worker removed. Apply: hermes gateway restart\n'
    else
      printf 'Plugin pi-worker left installed (disabled).\n'
    fi
  fi
fi

# --- CLI symlink ---------------------------------------------------------------
rm -f "${PI_BRIDGE_BIN:-$HOME/.local/bin/pi-bridge}" 2>/dev/null || true
printf 'CLI symlink removed (the checkout and its venv are untouched).\n'

# --- job state ------------------------------------------------------------------
# destructive on purpose: NEVER auto-confirmed, not even with PI_BRIDGE_YES/--yes
STATE_DIR="${PI_BRIDGE_HOME:-$HOME/.local/state/pi-bridge}"
case "$STATE_DIR" in
  ""|"/"|"$HOME")
    printf 'ERROR: refusing to treat %s as deletable bridge state. Set PI_BRIDGE_HOME to the exact bridge state dir.\n' "$STATE_DIR"
    exit 2 ;;
esac
printf 'Delete bridge job state (jobs/results/logs) in %s?\n' "$STATE_DIR"
printf 'Type DELETE to confirm (anything else keeps the state): '; read -r CONFIRM || CONFIRM=""
if [ "$CONFIRM" = "DELETE" ]; then
  rm -rf -- "$STATE_DIR"
  printf 'Bridge state deleted.\n'
else
  printf 'Bridge state kept (running/completed Pi jobs stay recoverable with pi-bridge list).\n'
fi

if ask "Delete this checkout (incl. .venv)?"; then
  printf 'Run: cd ~ && rm -rf %s\n' "$PWD"
fi
printf 'Done. Hermes, Pi, pi-open-agents and PI WEB were not modified (pi-open-agents is never auto-removed).\n'
if [ "$FAILED" = 1 ]; then
  printf 'WARNING: uninstall finished WITH ERRORS (see above) — re-check ~/.hermes/config.yaml and wake.json.\n'
  exit 1
fi
