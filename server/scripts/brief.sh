#!/usr/bin/env bash
# Morning brief (weekdays 07:15): today's live time, the best departure window
# in the next 3 hours, and the delta vs leaving at 8:00. Silent when there is
# nothing useful to say.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export DB_PATH="$PROJECT_DIR/data/commute.db"

# On the mac there is no .venv, so this falls through to Homebrew python3 via
# the PATH export above — identical to pre-port behavior. On Linux
# install.sh provisions .venv with httpx (PEP 668 blocks a
# system-wide pip install there).
PY="$PROJECT_DIR/.venv/bin/python3"; [ -x "$PY" ] || PY="python3"

# `-m app.notify_cli` below resolves the package from PYTHONPATH. The `-c`
# call right after it still uses an inline sys.path.insert of its own — the
# two invocation styles coexist here, they haven't been unified.
export PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"

BODY=$("$PY" -c "
import sys; sys.path.insert(0, '$PROJECT_DIR')
from app.brief import brief_text
t = brief_text()
print(t if t else '')
")

# The brief is informational, not an alert, so it must not inherit the alert
# addresses from a shared notifier config.
#
# BOTH --from and --to matter, for different reasons. --to picks the recipient-side
# rule that routes this to an inbox. --from is the one that actually decides the
# outcome: mail-filing services commonly sort by SENDER and re-learn from where a
# message lands, so anything sharing a sender with the alert stream gets dragged
# into the alert folder no matter what the recipient says. A dedicated sender is
# what keeps the two streams apart.
#
# Overriding per message rather than editing the shared config leaves any other
# project sharing it on the alert addresses, which is what they want.
BRIEF_ADDR="${BRIEF_ADDR:-commute@travismetcalf.com}"

if [ -n "$BODY" ]; then
  SUBJECT="Commute brief — $(TZ=America/New_York date '+%a %b %-d')"
  # One dispatcher call covers both channels. It exits non-zero only when a
  # channel we were ASKED to use failed — so the marker below still advances
  # when the brief is switched off, which is a completed run that correctly did
  # nothing (the same reasoning poll.sh uses for a deliberate window skip).
  if RESULT=$(printf '%s\n' "$BODY" | "$PY" -m app.notify_cli brief \
        --subject "$SUBJECT" --from "$BRIEF_ADDR" --to "$BRIEF_ADDR")
  then
    touch "$PROJECT_DIR/scripts/brief-last-success"
    STATUS="$RESULT"
  else
    STATUS="send-failed ${RESULT:-}"
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') brief $STATUS"
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') nothing to say yet"
fi
