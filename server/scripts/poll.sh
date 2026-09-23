#!/usr/bin/env bash
# Poll TomTom for live commute times. launchd runs this every 10 min around the
# clock; the Mon-Fri 07:00-19:00 ET WINDOW lives HERE, not in the plist — one
# line, DST-correct (the plist's calendar clock is not), and launchd coalesces
# missed StartInterval firings on wake into a single run instead of a burst.
# FORCE=1 bypasses the window for manual testing.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export DB_PATH="$PROJECT_DIR/data/commute.db"
TOMTOM_API_KEY="$(grep '^TOMTOM_API_KEY=' "$PROJECT_DIR/.env" | cut -d= -f2-)"
export TOMTOM_API_KEY

# On the mac there is no .venv, so this falls through to Homebrew python3 via
# the PATH export above — identical to pre-port behavior. On Linux
# install.sh provisions .venv with httpx (PEP 668 blocks a
# system-wide pip install there).
PY="$PROJECT_DIR/.venv/bin/python3"; [ -x "$PY" ] || PY="python3"

# For `-m app.notify_cli` below, which resolves the package from PYTHONPATH
# the same way brief.sh's does.
export PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"

if [ "${FORCE:-0}" != "1" ]; then
  DOW=$(TZ=America/New_York date +%u)   # 1=Mon .. 7=Sun
  HOUR=$(TZ=America/New_York date +%H)
  if [ "$DOW" -gt 5 ] || [ "$((10#$HOUR))" -lt 7 ] || [ "$((10#$HOUR))" -ge 19 ]; then
    # Heartbeat on window-skips too: the marker means "the poller RAN", not
    # "data is fresh" — so the dead-man threshold can stay 30 min around the
    # clock instead of having to absorb the 60h weekend gap.
    touch "$PROJECT_DIR/scripts/poll-last-success"
    exit 0
  fi
fi

# Bounded tile-cache growth (the only unbounded thing in data/).
find "$PROJECT_DIR/data/tilecache" -type f -mtime +30 -delete 2>/dev/null || true

FAILCOUNT_FILE="$PROJECT_DIR/scripts/poll-fail-count"
if ! out=$("$PY" "$PROJECT_DIR/scripts/poll.py" 2>&1); then
  # Email only after 3 consecutive failures — one transient TomTom 5xx or a
  # 30-second Wi-Fi blip must not generate 72 emails a day.
  n=$(( $(cat "$FAILCOUNT_FILE" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$FAILCOUNT_FILE"
  if [ "$n" -eq 3 ]; then
    # Through the dispatcher, not notify-email.sh directly, so
    # notify_system_email actually governs this alert. `|| true`: this is a
    # failure-path email, and a failing send must not change THIS script's
    # exit code — brief.sh's capture-and-branch shape is wrong here, because
    # nothing downstream keys a dead-man marker off this send the way
    # brief.sh's does off its own. stderr is NOT redirected to /dev/null,
    # only stdout is: the anomaly-check invocation below already learned
    # this lesson once (see its comment) — a raising dispatch whose stderr
    # goes to /dev/null is a diagnostic (here: notify_cli's own "database
    # unreadable, dispatching blind" line) silently eaten with no trace.
    printf '%s\n\n%s\n' "commute-tracker poller failed 3x in a row:" "$out" \
      | "$PY" -m app.notify_cli system \
          --subject "commute-tracker poller FAIL (3 consecutive)" \
          >/dev/null || true
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') $out" >&2
  exit 1
fi
rm -f "$FAILCOUNT_FILE"
touch "$PROJECT_DIR/scripts/poll-last-success"
echo "$(date '+%Y-%m-%d %H:%M:%S') $out"

# Anomaly check rides the poll cycle (no separate job): compares the newest
# sample to the trailing p90 for its (route, dow, slot). `|| true` means it
# never fails the poll — but stderr is NOT redirected: a traceback from a
# raising dispatch (bad JWT, locked DB, missing key file) must reach this
# job's log, not vanish. Previously it went to /dev/null, which is how a
# raising push once silently ate that day's anomaly alert with no trace.
"$PY" "$PROJECT_DIR/scripts/anomaly-check.py" || true
