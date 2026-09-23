#!/usr/bin/env bash
# Pre-deploy static checks. Touches nothing running.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
UNAME_S="$(uname -s)"
ERRORS=0

fail() { echo "  FAIL: $*"; ERRORS=$((ERRORS + 1)); }
pass() { echo "  OK:   $*"; }
info() { echo "  INFO: $*"; }

echo "=== commute-tracker validate ==="

if docker info >/dev/null 2>&1; then pass "Docker running"
else fail "Docker not running"; fi

if docker compose -f "$PROJECT_DIR/docker-compose.yml" config >/dev/null 2>&1; then
  pass "docker-compose.yml syntax valid"
else fail "docker-compose.yml syntax error"; fi

# The container could not send push for months because the compose file passed
# no APNS_* variables and .dockerignore keeps .env out of the image. Assert not
# just that the key is present but that it resolves to the in-container path —
# grep on the key name alone would still match a reverted ${APNS_KEY_PATH}
# pass-through, which is the exact regression this guard exists to catch.
COMPOSE_CONFIG=$(docker compose -f "$PROJECT_DIR/docker-compose.yml" config 2>/dev/null || true)

if echo "$COMPOSE_CONFIG" | grep -qE "APNS_KEY_PATH:[[:space:]]*/app/data/apns\.p8"; then
  pass "compose passes APNS_KEY_PATH as the in-container path"
else
  fail "compose does not pass APNS_KEY_PATH=/app/data/apns.p8 — container push will silently no-op"
fi

# APNS_KEY_PATH above is a hardcoded literal in docker-compose.yml — it can
# only fail if that one line is reverted. APNS_KEY_ID / APNS_TEAM_ID /
# APNS_BUNDLE_ID are the three that can actually render EMPTY (an unset or
# blank .env value substitutes to ""), and push.configured() only tests
# APNS_KEY_PATH and APNS_KEY_ID — a real key file plus a blank APNS_TEAM_ID
# still reports configured()==True, then builds a JWT with an empty issuer
# that APNs silently rejects. Assert all three resolve non-empty too.
for VAR in APNS_KEY_ID APNS_TEAM_ID APNS_BUNDLE_ID; do
  LINE=$(echo "$COMPOSE_CONFIG" | grep -E "^[[:space:]]*${VAR}:" | head -1 || true)
  VALUE=$(echo "$LINE" | sed -E "s/^[[:space:]]*${VAR}:[[:space:]]*//; s/[[:space:]]*\$//")
  if [[ -n "$VALUE" && "$VALUE" != '""' && "$VALUE" != "''" && "$VALUE" != "null" ]]; then
    pass "compose resolves $VAR (non-empty)"
  else
    fail "compose resolves $VAR to empty — push will silently fail APNs auth"
  fi
done

ENV_FILE="$PROJECT_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
  pass ".env exists"
  # BSD stat (-f %OLp) on macOS, GNU stat (-c %a) on Linux — both print octal perms.
  if [[ "$UNAME_S" == "Darwin" ]]; then
    PERMS=$(stat -f "%OLp" "$ENV_FILE")
  else
    PERMS=$(stat -c "%a" "$ENV_FILE")
  fi
  if [[ "$PERMS" == "600" ]]; then pass ".env permissions 600"
  else fail ".env permissions are $PERMS (want 600)"; fi

  TOMTOM=$(grep '^TOMTOM_API_KEY=' "$ENV_FILE" | cut -d= -f2- || true)
  if [[ -n "$TOMTOM" ]]; then pass "TOMTOM_API_KEY set"
  else fail "TOMTOM_API_KEY empty — get one at developer.tomtom.com"; fi

  TOKEN=$(grep '^API_BEARER_TOKEN=' "$ENV_FILE" | cut -d= -f2- || true)
  if [[ ${#TOKEN} -ge 32 ]]; then pass "API_BEARER_TOKEN >= 32 chars"
  else fail "API_BEARER_TOKEN too short (${#TOKEN} chars; install.sh generates one)"; fi
else
  fail ".env missing — copy .env.example, chmod 600, fill from your password manager"
fi

# Schema sanity: init against a throwaway DB catches typos before the container.
export PROJECT_DIR
if DB_PATH=$(mktemp -u /tmp/ct-validate-XXXXXX.db) python3 - <<'EOF' 2>/dev/null
import os, sys
sys.path.insert(0, os.environ["PROJECT_DIR"])
from app.db import init_db
init_db()
EOF
then pass "schema initializes clean"
else fail "app.db.init_db() failed — schema error"; fi

# Places seeded with plausible coordinates
DB="$PROJECT_DIR/data/commute.db"
if [[ -f "$DB" ]]; then
  PLACES=$(sqlite3 -readonly "file:$DB?mode=ro" "SELECT COUNT(*) FROM places" 2>/dev/null || echo 0)
  if [[ "${PLACES:-0}" -ge 2 ]]; then pass "places seeded ($PLACES)"
  else info "places not yet seeded — set them at /settings before the first poll"; fi
else
  info "no database yet (first run)"
fi

for script in install.sh smoke.sh poll.sh backup.sh restore-test.sh derive-trips.sh brief.sh; do
  if [[ -x "$SCRIPT_DIR/$script" ]]; then pass "$script executable"
  else fail "$script not executable"; fi
done

echo ""
if [[ $ERRORS -eq 0 ]]; then echo "All validation checks passed."; exit 0
else echo "$ERRORS validation check(s) failed."; exit 1; fi
