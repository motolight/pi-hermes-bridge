#!/usr/bin/env bash
# pi-hermes-bridge v0.2 installer — guided one-command setup.
#
# Checks prerequisites, installs the bridge CLI + the pi-worker Hermes plugin,
# and configures the Hermes side for you: the routing policy (managed SOUL.md
# block + pi-routing-policy skill) and the completion wake channel (HMAC
# webhook route + wake.json).  No manual SOUL.md or webhook editing needed on
# the normal path.
#
# It NEVER installs or updates Hermes, Pi or PI WEB (security: those are your
# installations).  pi-open-agents MAY be offered as a separate, consented
# `pi install npm:pi-open-agents` (no sudo, no system packages).
# Every Hermes-side change is owned, idempotent and removable by uninstall.sh.
#
# Flags/env:
#   --yes / PI_BRIDGE_YES=1     non-interactive, answer yes to offers
#   --no-restart                skip the controlled `hermes gateway restart`
#   PI_BRIDGE_PYTHON=python3    python interpreter for the venv
#   PI_BRIDGE_PI_BIN=/path/pi   explicit pi binary
#   PI_BRIDGE_WAKE_PORT=8644    wake webhook port (existing platform port wins)
#   PI_BRIDGE_SKIP_TESTS=1      skip the end-of-install test run
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say(){ printf '\n== %s\n' "$*"; }
die(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
warn(){ printf 'WARNING: %s\n' "$*" >&2; }
ASK_YES="${PI_BRIDGE_YES:-0}"
ask(){
  if [ "$ASK_YES" = 1 ]; then printf '%s [y/N] y (auto)\n' "$*"; return 0; fi
  printf '%s [y/N] ' "$*"; local a; read -r a || a=""
  [[ "$a" == y || "$a" == Y ]]
}

NO_RESTART=0
for a in "$@"; do
  case "$a" in
    --yes|-y) ASK_YES=1 ;;
    --no-restart) NO_RESTART=1 ;;
    --help|-h) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown flag: $a (try --help)" ;;
  esac
done

PYTHON="${PI_BRIDGE_PYTHON:-${PYTHON:-python3}}"
WAKE_PORT="${PI_BRIDGE_WAKE_PORT:-8644}"
HERMES_URL="https://github.com/NousResearch/hermes-agent"
PI_URL="https://github.com/earendil-works/pi"
BRIDGE_PY=""   # venv python, set in step 3

# ---------------------------------------------------------------------------
say "1/8 prerequisites"
command -v "$PYTHON" >/dev/null || die "python3 not found — the bridge needs Python >= 3.10 for its venv."
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' \
  || die "Python >= 3.10 required, found: $("$PYTHON" -V 2>&1)"

HAVE_HERMES=0; command -v hermes >/dev/null 2>&1 && HAVE_HERMES=1
[ "$HAVE_HERMES" = 1 ] || die "Hermes Agent CLI ('hermes') not found.
The installer deliberately does NOT install Hermes (security: it is your
agent, with your credentials and configuration).
Install it separately first:  ${HERMES_URL}
Then re-run ./install.sh"

PI_BIN="${PI_BRIDGE_PI_BIN:-$(command -v pi || true)}"
[ -n "$PI_BIN" ] || die "Pi Coding Agent CLI ('pi') not found.
The installer deliberately does NOT install Pi (security: it runs with your
model credentials).
Install it separately first:  ${PI_URL}
Then re-run ./install.sh (or export PI_BRIDGE_PI_BIN=/path/to/pi)."
"$PI_BIN" --version >/dev/null 2>&1 || die "'$PI_BIN' is not runnable"
printf 'Pi OK: %s\n' "$("$PI_BIN" --version | head -1)"

# systemd user session: required for durable jobs (systemd-run --user).
HAVE_SYSTEMD=0
[ -n "${XDG_RUNTIME_DIR:-}" ] && systemctl --user show --property=DefaultTarget >/dev/null 2>&1 && HAVE_SYSTEMD=1
[ "$HAVE_SYSTEMD" = 1 ] || printf 'NOTE: no usable systemd user session -> Pi jobs run detached and will NOT survive logout/reboot. Start one with: loginctl enable-linger $USER\n'

# ---------------------------------------------------------------------------
say "2/8 Pi print-mode + agent/orchestrator capability"
PI_HELP="$("$PI_BIN" --help 2>&1 || true)"
for flag in --print --session-id --session-dir; do
  printf '%s\n' "$PI_HELP" | grep -q -- "$flag" \
    || die "Pi at $PI_BIN lacks $flag — the bridge requires pi print mode (--print --session-id --session-dir). Update Pi: $PI_URL"
done
AGENT_FLAG=0
printf '%s\n' "$PI_HELP" | grep -q -- '--agent' && AGENT_FLAG=1
[ "$AGENT_FLAG" = 1 ] || die "Pi at $PI_BIN has no --agent support at all.
The bridge always runs 'pi --agent orchestrator'; --agent is provided by the
pi-open-agents extension (or an equivalent), not by pi core. Install it with:
    pi install npm:pi-open-agents
(or see $PI_URL), then re-run ./install.sh"

PI_AGENT_HOME="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
have_orchestrator=0
[ -f "$PI_AGENT_HOME/agents/orchestrator.md" ] && have_orchestrator=1
have_open_agents=0
if [ -e "$PI_AGENT_HOME/npm/node_modules/pi-open-agents" ] \
   || grep -qs "pi-open-agents" "$PI_AGENT_HOME/settings.json" "$HOME/.pi/settings.json" 2>/dev/null; then
  have_open_agents=1
fi

if [ "$have_orchestrator" = 1 ]; then
  printf 'Existing orchestrator agent found (%s/agents/orchestrator.md) — nothing to change.\n' "$PI_AGENT_HOME"
else
  if [ "$have_open_agents" = 0 ]; then
    printf '\npi-open-agents is not installed. It is the standard pi extension that\n'
    printf 'provides named agents (--agent) and the explorer/worker/reviewer roles.\n'
    printf 'The installer would run exactly:\n\n    %s install npm:pi-open-agents\n\n' "$PI_BIN"
    printf '(uses the built-in package mechanism of pi: no sudo, no system\n packages; it adds the extension to your pi settings, touching no existing configs)\n'
    if ask "Install pi-open-agents now?"; then
      "$PI_BIN" install npm:pi-open-agents || die "pi install npm:pi-open-agents failed.
Install pi-open-agents yourself (see its docs), then re-run ./install.sh."
      have_open_agents=1
      printf 'pi-open-agents installed; re-checking capability...\n'
      PI_HELP="$("$PI_BIN" --help 2>&1 || true)"
      printf '%s\n' "$PI_HELP" | grep -q -- '--agent' \
        || die "after installing pi-open-agents, pi still does not expose --agent; something is off with the pi setup. Re-run: pi --help"
    else
      printf 'Skipped — pi-open-agents is REQUIRED for the bridge to run jobs at all.\n'
    fi
  fi
  if [ "$have_open_agents" = 0 ]; then
    die "No Pi orchestrator and pi-open-agents declined — the bridge cannot delegate without 'pi --agent orchestrator'.
Set it up yourself (any agent named 'orchestrator' works; the bridge never
prescribes its model or subagents), then re-run ./install.sh."
  fi
  # orchestrator missing: pi-open-agents is now present (installed or already
  # was) -> offer to create the minimal agent file.
  if [ "$have_orchestrator" = 0 ]; then
    printf '\nNo orchestrator agent found at %s/agents/orchestrator.md.\n' "$PI_AGENT_HOME"
    printf 'The installer can create a MINIMAL orchestrator using the standard\npi-open-agents format. It prescribes NO model (the agent uses your\ncurrent/default pi model) and no subagent choices beyond making the standard\nroles available. It will not overwrite anything (the file must not exist).\n\n'
    if ask "Create the minimal orchestrator agent now?"; then
      [ -e "$PI_AGENT_HOME/agents/orchestrator.md" ] && die "orchestrator.md appeared concurrently; nothing overwritten."
      mkdir -p "$PI_AGENT_HOME/agents"
      cat > "$PI_AGENT_HOME/agents/orchestrator.md" <<'ORCH'
---
name: orchestrator
description: Main autonomous coding agent (created by the pi-hermes-bridge installer; takes Pi's current/default model).
mode: primary
allowedAgents: [explorer, worker, reviewer]
permission:
  "*": allow
---

You are the Pi orchestrator for delegated jobs.

Before changing anything, inspect the project (files, git status, relevant docs)
and understand the existing architecture.

Take ownership of the complete task: plan, implement, run the relevant tests,
fix regressions your change caused, and use subagents (explorer / worker /
reviewer) where they genuinely help. You decide the team — the requester never
prescribes it.

Work until the acceptance criteria in the task are met, then end your turn with
a short, honest report of what changed, what was verified, and what is left.
ORCH
      printf 'Created %s/agents/orchestrator.md\n' "$PI_AGENT_HOME"
    else
      printf 'Skipped creating the orchestrator.\n'
    fi
    have_orchestrator=0
    [ -f "$PI_AGENT_HOME/agents/orchestrator.md" ] && have_orchestrator=1
    [ "$have_orchestrator" = 1 ] || die "The bridge always runs 'pi --agent orchestrator', so an 'orchestrator' agent must exist.
Create it yourself (minimal pi-open-agents format: frontmatter with
'name: orchestrator', 'mode: primary', 'allowedAgents: [explorer, worker,
reviewer]' in %s/agents/orchestrator.md — no model line needed; pi uses your
default), then re-run ./install.sh. See docs/PI_ORCHESTRATOR_EXAMPLE.md" "$PI_AGENT_HOME"
  fi
fi

# Real availability probe: the orchestrator must actually answer, not just
# exist on disk.  A model failure is NOT fatal here (your model choice is
# yours); everything else still installs.
ORCH_PROBE=0
if timeout 60 "$PI_BIN" --print --agent orchestrator "reply with exactly OK" </dev/null >/dev/null 2>&1; then
  ORCH_PROBE=1
  printf 'Pi orchestrator probe: OK\n'
else
  printf 'NOTE: `pi --print --agent orchestrator` did not complete a trivial turn. The integration will install, but delegate jobs will fail until a working model is configured for pi (check `pi` normally once: pi --agent orchestrator). This installer does not configure models.\n'
fi

# ---------------------------------------------------------------------------
say "3/8 python venv + bridge CLI"
[ -d .venv ] || "$PYTHON" -m venv .venv
.venv/bin/pip install -q --upgrade pip >/dev/null
PIP_NET="--timeout 60 --retries 3"
.venv/bin/pip install -q $PIP_NET -e . || die "pip install -e . failed (network needed for the ruamel.yaml dependency; or pre-populate the pip cache)"
.venv/bin/pi-bridge --help >/dev/null || die "pi-bridge CLI did not install cleanly"
BRIDGE_PY="$PWD/.venv/bin/python"
hs(){ "$BRIDGE_PY" -m pi_bridge hermes-setup "$@"; }
jget(){ "$BRIDGE_PY" -c 'import json,sys;d=json.load(sys.stdin)
k=sys.argv[1].split(".")
for p in k: d=d.get(p) if isinstance(d,dict) else None
print("" if d is None else d)' "$1"; }
# Make the CLI reachable from the Hermes gateway process: the pi-worker
# plugin resolves it via $PI_BRIDGE_CLI -> PATH -> ~/.local/bin/pi-bridge.
if [ -w "$HOME/.local/bin" ] || mkdir -p "$HOME/.local/bin" 2>/dev/null; then
  ln -sf "$PWD/.venv/bin/pi-bridge" "$HOME/.local/bin/pi-bridge" \
    && printf 'pi-bridge CLI: %s/.venv/bin/pi-bridge (linked at ~/.local/bin/pi-bridge)\n' "$PWD" \
    || warn "could not link ~/.local/bin/pi-bridge; set PI_BRIDGE_CLI=$PWD/.venv/bin/pi-bridge for the gateway environment."
else
  warn "cannot create ~/.local/bin; set PI_BRIDGE_CLI=$PWD/.venv/bin/pi-bridge for the gateway environment."
fi
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) printf 'NOTE: add ~/.local/bin to PATH (also in the environment the Hermes gateway runs in) so the pi-worker plugin finds pi-bridge.\n' ;;
esac

# ---------------------------------------------------------------------------
say "4/8 record pi binary (discovery + capability check -> bridge config)"
.venv/bin/pi-bridge install --pi-bin "$PI_BIN" --json >/dev/null || die "pi capability check failed for $PI_BIN"

# ---------------------------------------------------------------------------
say "5/8 Hermes plugin pi-worker"
CHANGED=0
if [ ! -d "$HOME/.hermes/plugins/pi-worker" ] || \
   ! diff -rq --exclude=__pycache__ plugin "$HOME/.hermes/plugins/pi-worker" >/dev/null 2>&1; then
  CHANGED=1
fi
mkdir -p "$HOME/.hermes/plugins/pi-worker"
cp -R plugin/. "$HOME/.hermes/plugins/pi-worker/"
rm -rf "$HOME/.hermes/plugins/pi-worker/__pycache__"
printf 'Plugin copied to ~/.hermes/plugins/pi-worker.\n'
for t in pi_delegate pi_status pi_feedback pi_cancel pi_list; do
  grep -q "  - $t" "$HOME/.hermes/plugins/pi-worker/plugin.yaml" \
    || die "plugin manifest at $HOME/.hermes/plugins/pi-worker is missing tool $t"
done
printf 'Manifest provides all 5 tools: pi_delegate pi_status pi_feedback pi_cancel pi_list\n'
hermes plugins enable pi-worker >/dev/null 2>&1 \
  || warn "'hermes plugins enable pi-worker' returned an error; enable manually: hermes plugins enable pi-worker"
"$BRIDGE_PY" - <<'PY'
from pi_bridge import hermes_setup as h
st = h.load_state(); st["plugin_installed"] = True; h.save_state(st)
PY
printf 'Plugin ownership recorded for uninstall.\n'

# ---------------------------------------------------------------------------
say "6/8 routing policy for Hermes (SOUL.md managed block + skill)"
SOUL_RES=$(hs soul-install) || die "SOUL.md routing block installation failed: $SOUL_RES"
SOUL_ACTION=$(printf '%s' "$SOUL_RES" | jget soul)
printf 'SOUL.md routing block: %s (managed between pi-hermes-bridge markers; existing content preserved)\n' "$SOUL_ACTION"
case "$SOUL_ACTION" in created|updated) CHANGED=1 ;; esac
SKILL_RES=$(hs skill-install) || warn "skill install failed: $SKILL_RES"
SKILL_ACTION=$(printf '%s' "$SKILL_RES" | jget skill)
printf 'pi-routing-policy skill: %s\n' "$SKILL_ACTION"
case "$SKILL_ACTION" in created|updated) CHANGED=1 ;; esac

# ---------------------------------------------------------------------------
say "7/8 completion wake channel (webhook route + wake.json)"
ROUTE_RES=$(hs route-install --port "$WAKE_PORT" --json) \
  || die "could not configure the wake webhook route in ~/.hermes/config.yaml:
$ROUTE_RES
The route was NOT added and other Hermes settings are untouched; anything
installed by earlier steps remains installed and ./uninstall.sh removes it."
ROUTE_ACTION=$(printf '%s' "$ROUTE_RES" | jget route)
WAKE_PORT=$(printf '%s' "$ROUTE_RES" | jget port)
printf 'webhook route pi-bridge-complete: %s (port %s, bind 127.0.0.1, deliver=log, origin-aware prompt V1.3)\n' "$ROUTE_ACTION" "$WAKE_PORT"
case "$ROUTE_ACTION" in created_route|updated_route|created_platform_and_route) CHANGED=1 ;; esac
[ "$ROUTE_ACTION" = "kept_manual" ] && printf 'NOTE: a pre-existing manual pi-bridge-complete route was found and LEFT UNTOUCHED (no duplicate created). Its prompt may be an older revision — review it against docs/WAKE_SETUP.md if wakes behave unexpectedly.\n'
WAKE_RES=$(hs wake-write --port "$WAKE_PORT" --json) \
  || die "wake.json could not be written: $WAKE_RES"
WAKE_ACTION=$(printf '%s' "$WAKE_RES" | jget wake_json)
printf 'wake.json: %s\n' "$WAKE_ACTION"
# never claim 'ready' with a wake channel that cannot authenticate: a foreign
# wake.json that disagrees with the route we just wrote would 401 forever
if [ "$WAKE_ACTION" = "kept_existing" ]; then
  "$BRIDGE_PY" - <<'PY' || die "existing ${PI_BRIDGE_HOME:-$HOME/.local/state/pi-bridge}/wake.json was not created by this installer AND does not match the configured route (url/secret) — wakes would silently fail with 401. Nothing was changed: review that file (or delete it) and re-run ./install.sh to let the installer manage the wake channel."
import json, sys
from pi_bridge import hermes_setup as h
try:
    d = json.loads(h.wake_json_path().read_text())
    sec, _ = h.route_secret_in_config()
    owned = bool(h.load_state().get("wake", {}).get("route_owned"))
    ok = (not owned) or (d.get("url") == h.wake_url(int(h.load_state()["wake"].get("port", 8644)))
                         and d.get("secret") and d.get("secret") == sec)
except Exception:
    ok = False
sys.exit(0 if ok else 1)
PY
fi

# ---------------------------------------------------------------------------
say "8/8 apply + verify (controlled gateway restart)"
GATEWAY_NOTE="the bridge config is applied when the gateway (re)starts"
if [ "$NO_RESTART" = 1 ]; then
  printf 'Skipped restart (--no-restart). Apply changes when convenient: hermes gateway restart\n'
elif [ "$CHANGED" != 1 ]; then
  printf 'Nothing changed since the previous install — no gateway restart needed.\n'
  GATEWAY_NOTE="nothing changed; no restart performed"
elif pgrep -u "$("$BRIDGE_PY" -c 'import os;print(os.getuid())')" -f "hermes.*gateway" >/dev/null 2>&1; then
  printf 'A hermes gateway is running -> hermes gateway restart (one controlled restart, required to load the plugin + static route)\n'
  if hermes gateway restart >/dev/null 2>&1; then
    UP=0
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
      if "$BRIDGE_PY" - "$WAKE_PORT" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket(); s.settimeout(0.5)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
      then UP=1; break; fi
      sleep 2
    done
    if [ "$UP" = 1 ]; then
      GATEWAY_NOTE="gateway restarted, webhook port $WAKE_PORT listening"
    else
      warn "gateway restarted but the webhook port is not answering yet; check: hermes gateway status / journalctl --user -u hermes-gateway -n 40"
      GATEWAY_NOTE="gateway restart issued; webhook port NOT verified yet — check 'hermes gateway status'"
    fi
  else
    warn "'hermes gateway restart' failed; apply manually: hermes gateway restart"
    GATEWAY_NOTE="gateway restart FAILED — run 'hermes gateway restart' yourself"
  fi
else
  printf 'No running hermes gateway detected — nothing to restart; new config applies on first start.\n'
fi

# ---------------------------------------------------------------------------
if [ "${PI_BRIDGE_SKIP_TESTS:-0}" != 1 ]; then
  say "self-test"
  .venv/bin/python -m pip install -q pytest >/dev/null 2>&1 || true
  SELFTEST_LOG="$(mktemp)"
  if .venv/bin/python -m pytest tests/ -q >"$SELFTEST_LOG" 2>&1; then
    tail -1 "$SELFTEST_LOG"
  else
    warn "self-test FAILED — see $SELFTEST_LOG before trusting the install (nothing else was changed by this run)"
  fi
fi

say "done"
cat <<EOF
Summary:
  * bridge CLI + pi-worker plugin: installed, plugin enabled
  * routing policy: SOUL.md managed block ($(printf '%s' "$SOUL_RES" | jget soul)), skill $(printf '%s' "$SKILL_RES" | jget skill)
  * completion wake: route '$ROUTE_ACTION' on 127.0.0.1:$WAKE_PORT, wake.json $(printf '%s' "$WAKE_RES" | jget wake_json)
  * gateway: $GATEWAY_NOTE
EOF
if [ "$ORCH_PROBE" = 1 ]; then
  printf '\npi-hermes-bridge is ready. Hermes can now delegate substantial coding/DevOps work to Pi automatically.\n'
else
  printf '\npi-hermes-bridge is installed. Finish the NOTE above (Pi model / orchestrator probe), then verify with: pi-bridge list\n'
fi
