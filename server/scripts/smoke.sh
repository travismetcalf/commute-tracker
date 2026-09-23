#!/usr/bin/env bash
# Daily smoke (09:55). Sources the shared harness; keeps only its own checks.
set -u
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SMOKE_LABEL="commute-tracker"
# Optional shared harness when present (adds -q, email-on-fail, success
# markers); minimal built-in fallback otherwise so the script runs anywhere,
# which is the supported path for a standalone deployment.
if [ -f "$HOME/projects/_shared/scripts/smoke-lib.sh" ]; then
  source "$HOME/projects/_shared/scripts/smoke-lib.sh" "$@"   # MUST pass "$@"
else
  FAILS=0
  section() { echo "== $* =="; }
  pass()    { echo "  PASS  $*"; }
  fail()    { echo "  FAIL  $*"; FAILS=$((FAILS+1)); }
  # Mirror smoke-lib's touch-on-pass so an external dead-man check still gets
  # a heartbeat when the shared harness isn't deployed (the whole reason this
  # fallback exists), not just on the sourced-lib path.
  smoke_finish() { if [ "$FAILS" -eq 0 ]; then echo "All checks passed."; touch "$PROJECT_DIR/scripts/smoke-last-success"; exit 0; else echo "$FAILS check(s) FAILED."; exit 1; fi; }
fi
# /usr/local/bin(:/opt/homebrew) first keeps macOS's OrbStack docker resolution
# order unchanged; appending /usr/bin:/bin is what lets this find docker CE on
# Linux, where it lives at /usr/bin/docker.
export PATH="/usr/local/bin:$PATH:/usr/bin:/bin"
UNAME_S="$(uname -s)"

env_val() { grep "^$1=" "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2- || true; }
MDNS_HOSTNAME="$(env_val MDNS_HOSTNAME)"
LABEL_PREFIX="$(env_val LAUNCHD_LABEL_PREFIX)"; LABEL_PREFIX="${LABEL_PREFIX:-local}"

DB="$PROJECT_DIR/data/commute.db"
DOCKER="$(command -v docker || echo /usr/local/bin/docker)"

section "container"
if $DOCKER ps --format '{{.Names}}' 2>/dev/null | grep -q '^commute-tracker$'; then
  pass "container running"
  HEALTH=$($DOCKER inspect -f '{{.State.Health.Status}}' commute-tracker 2>/dev/null || echo unknown)
  if [ "$HEALTH" = "healthy" ]; then pass "healthcheck healthy"
  else fail "healthcheck: $HEALTH"; fi
else
  fail "container not running"
fi

section "http"
if curl -fsS -m 5 http://localhost:8085/health >/dev/null 2>&1; then
  pass "localhost:8085/health responds"
else fail "localhost:8085/health unreachable"; fi
# The .local name is advertised by the mac's dns-sd job and served by the mac's
# Caddy; a headless Linux host has no mDNS resolver and is not the
# host the LAN URL is meant to be tested from — check it on Darwin only.
if [ "$UNAME_S" != "Darwin" ]; then
  pass "LAN hostname check skipped on Linux (mDNS advertiser + Caddy stay on the macOS edge host)"
elif [ -n "$MDNS_HOSTNAME" ]; then
  if curl -fsS -m 5 -o /dev/null "http://$MDNS_HOSTNAME/" 2>/dev/null; then
    pass "$MDNS_HOSTNAME serves (LAN reverse-proxy OK)"
  else fail "$MDNS_HOSTNAME unreachable"; fi
else
  pass "LAN hostname check skipped (MDNS_HOSTNAME unset)"
fi

section "database"
if [ -f "$DB" ]; then
  INTEGRITY=$(sqlite3 "$DB" ".timeout 5000" "PRAGMA integrity_check;" 2>/dev/null || echo error)
  if [ "$INTEGRITY" = "ok" ]; then pass "integrity_check ok"
  else fail "integrity_check: $INTEGRITY"; fi
else
  fail "database missing at $DB"
fi

section "poller"
# Freshness asserted only inside the Mon-Fri 07:00-19:00 window (09:55 is
# inside it on weekdays). Outside the window: report-only.
NEWEST=$(sqlite3 "$DB" ".timeout 5000" \
  "SELECT ts_utc FROM predicted_samples WHERE kind='live' ORDER BY ts_utc DESC LIMIT 1" 2>/dev/null || true)
DOW=$(TZ=America/New_York date +%u); HOUR=$(TZ=America/New_York date +%H)
IN_WINDOW=0
[ "$DOW" -le 5 ] && [ "$((10#$HOUR))" -ge 7 ] && [ "$((10#$HOUR))" -lt 19 ] && IN_WINDOW=1
if [ -n "$NEWEST" ]; then
  # BSD date (-j -f) on macOS needs an explicit format for the ts_utc string
  # ("YYYY-MM-DDTHH:MM:SSZ"); GNU date (-d) on Linux parses ISO-8601 directly.
  if [ "$UNAME_S" = "Darwin" ]; then
    SAMPLE_EPOCH=$(TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%SZ" "$NEWEST" +%s)
  else
    SAMPLE_EPOCH=$(date -u -d "$NEWEST" +%s)
  fi
  AGE_MIN=$(( ( $(date -u +%s) - SAMPLE_EPOCH ) / 60 ))
  if [ "$IN_WINDOW" -eq 1 ]; then
    if [ "$AGE_MIN" -le 25 ]; then pass "newest live sample ${AGE_MIN}m old"
    else fail "newest live sample ${AGE_MIN}m old (>25m inside the window)"; fi
  else
    pass "newest live sample ${AGE_MIN}m old (outside window, report-only)"
  fi
else
  if [ "$IN_WINDOW" -eq 1 ]; then fail "no live samples at all"
  else pass "no live samples yet (outside window, report-only)"; fi
fi

section "api budget"
MONTH=$(TZ=America/New_York date +%Y-%m)
CALLS=$(sqlite3 "$DB" ".timeout 5000" \
  "SELECT calls FROM api_usage WHERE month='$MONTH'" 2>/dev/null || echo 0)
CALLS=${CALLS:-0}
CAP=$(sqlite3 "$DB" ".timeout 5000" \
  "SELECT value FROM settings WHERE key='monthly_call_cap'" 2>/dev/null || echo 18000)
CAP=${CAP:-18000}
# NEVER let a DB string reach arithmetic expansion: bash re-evaluates a
# variable's CONTENTS inside $(( )), and array subscripts there undergo command
# substitution — a settings value like HOME[$(curl …|sh)] would execute here as
# the logged-in user. Same for CALLS, which the poller writes.
case "$CAP" in ''|*[!0-9]*) CAP=18000 ;; esac
case "$CALLS" in ''|*[!0-9]*) CALLS=0 ;; esac
if [ "$CALLS" -ge "$CAP" ]; then fail "API budget exhausted: $CALLS/$CAP"
elif [ "$CALLS" -ge $(( CAP * 8 / 10 )) ]; then fail "API budget past 80%: $CALLS/$CAP"
else pass "API budget: $CALLS/$CAP this month"; fi

section "ingest (report-only — PTO and WFH are legitimate)"
WEEK_EVENTS=$(sqlite3 "$DB" ".timeout 5000" \
  "SELECT COUNT(*) FROM trip_events WHERE device_ts > strftime('%s','now') - 7*86400" 2>/dev/null || echo 0)
pass "trip events in last 7 days: ${WEEK_EVENTS:-0}"

section "mdns"
# The mDNS advertiser is a launchd LaunchAgent — Darwin-only by construction.
# On Linux it stays on the macOS edge host; report that rather than fail.
if [ "$UNAME_S" != "Darwin" ]; then
  pass "mDNS advertiser stays on the macOS edge host (not applicable on Linux)"
elif [ -z "$MDNS_HOSTNAME" ]; then
  pass "mDNS agent skipped (not configured)"
elif launchctl list 2>/dev/null | grep -q "$LABEL_PREFIX.commute-tracker-mdns"; then
  pass "mDNS agent loaded"
else fail "mDNS agent not loaded"; fi

section "backup"
MARKER="$PROJECT_DIR/scripts/backup-last-success"
if [ ! -f "$PROJECT_DIR/scripts/backup.sh" ]; then
  pass "backup checks skipped (no backup job configured)"
elif [ -f "$MARKER" ]; then
  # BSD stat (-f %m) on macOS, GNU stat (-c %Y) on Linux — both print an mtime epoch.
  if [ "$UNAME_S" = "Darwin" ]; then
    MARKER_MTIME=$(stat -f %m "$MARKER")
  else
    MARKER_MTIME=$(stat -c %Y "$MARKER")
  fi
  AGE=$(( $(date +%s) - MARKER_MTIME ))
  if [ "$AGE" -lt 93600 ]; then pass "backup marker $((AGE/3600))h old"
  else fail "backup marker $((AGE/3600))h old (>26h)"; fi
else
  fail "backup has never succeeded (marker missing)"
fi

smoke_finish
