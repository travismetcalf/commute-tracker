#!/usr/bin/env bash
# Idempotent one-shot bring-up. Based on the house
# install.sh, upgraded to modern launchctl bootout/bootstrap.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
UNAME_S="$(uname -s)"
# command -v resolves via PATH above, so this still finds /usr/local/bin/docker
# first on macOS (byte-identical) while also resolving docker CE's
# /usr/bin/docker on Linux.
DOCKER="$(command -v docker || echo /usr/local/bin/docker)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

log() { echo "[install] $*"; }

# ── .env ─────────────────────────────────────────────────────────────────
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  log "created .env from template — fill TOMTOM_API_KEY by hand from your password manager"
fi
chmod 600 "$PROJECT_DIR/.env"

# Generate the bearer token once; print it exactly once so it can be pasted
# into your password manager and, later, the iOS app.
TOKEN=$(grep '^API_BEARER_TOKEN=' "$PROJECT_DIR/.env" | cut -d= -f2- || true)
if [[ -z "$TOKEN" ]]; then
  TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  python3 - "$PROJECT_DIR/.env" "$TOKEN" <<'EOF'
import sys
path, token = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines()
out = [f"API_BEARER_TOKEN={token}" if l.startswith("API_BEARER_TOKEN=") else l for l in lines]
open(path, "w").write("\n".join(out) + "\n")
EOF
  log "generated API_BEARER_TOKEN — store it in your password manager:"
  echo "    $TOKEN"
fi

mkdir -p "$PROJECT_DIR/data/tilecache"

# Host-side python dep for the poller/backfill (the container has its own).
# Darwin: Homebrew python3 accepts --break-system-packages for a single-user
# dev box. Linux: Debian 13's python3 is PEP 668
# externally-managed, so --break-system-packages is refused outright — a venv
# is the supported path, and python3-venv is already installed there. PY in
# poll.sh/derive-trips.sh/brief.sh picks up this venv automatically; there is
# nothing to activate here.
if [[ "$UNAME_S" == "Darwin" ]]; then
  python3 -c "import httpx" 2>/dev/null || {
    log "installing httpx for the host poller"
    pip3 install --break-system-packages --quiet httpx
  }
else
  if [[ ! -x "$PROJECT_DIR/.venv/bin/python3" ]]; then
    log "creating host venv for the poller (Linux/PEP 668)"
    python3 -m venv "$PROJECT_DIR/.venv"
  fi
  "$PROJECT_DIR/.venv/bin/python3" -c "import httpx" 2>/dev/null || {
    log "installing httpx into $PROJECT_DIR/.venv"
    "$PROJECT_DIR/.venv/bin/pip" install -q httpx
  }
fi

# ── validate before touching anything running ────────────────────────────
bash "$SCRIPT_DIR/validate.sh" || { log "validate failed — aborting"; exit 1; }

# ── host schedules ───────────────────────────────────────────────────────
# systemd user timers rendered from a fleet manifest in a separate ops repo. The
# mac-era launchd templates are parked in launchd/retired/ (2026-09-08) and
# are no longer rendered — there is no mac deployment left to roll back to.
log "host schedules come from the fleet manifest (systemd timers on the Linux host) — nothing to render"

# ── container ────────────────────────────────────────────────────────────
cd "$PROJECT_DIR"
# Darwin deploys the mac base file only (unchanged behaviour); Linux
# layers compose.p3.yml on top for the 0.0.0.0 port bind and uid 1000 the
# tailnet-crossing Caddy setup needs — the same -f ordering the other
# services on this host use.
if [[ "$UNAME_S" == "Darwin" ]]; then
  COMPOSE_FILES="-f docker-compose.yml"
elif [[ -f "$PROJECT_DIR/compose.p3.yml" ]]; then
  COMPOSE_FILES="-f docker-compose.yml -f compose.p3.yml"
else
  COMPOSE_FILES="-f docker-compose.yml"
fi
"$DOCKER" compose $COMPOSE_FILES up -d --build

log "waiting for healthy ..."
for i in $(seq 1 24); do
  H=$("$DOCKER" inspect -f '{{.State.Health.Status}}' commute-tracker 2>/dev/null || echo starting)
  [[ "$H" == "healthy" ]] && break
  sleep 5
done
[[ "${H:-}" == "healthy" ]] || { log "container never went healthy"; exit 1; }
log "container healthy"

bash "$SCRIPT_DIR/smoke.sh" -q && log "smoke passed" || { log "smoke FAILED"; exit 1; }
log "install complete"
