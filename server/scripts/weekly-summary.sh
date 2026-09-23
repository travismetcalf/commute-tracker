#!/usr/bin/env bash
# Weekly commute summary (Saturday mornings): Monday-Friday of the week just
# ending. Silent when the week had no qualifying trips.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export DB_PATH="$PROJECT_DIR/data/commute.db"

PY="$PROJECT_DIR/.venv/bin/python3"; [ -x "$PY" ] || PY="python3"
export PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"

HTML_FILE="$(mktemp)"
trap 'rm -f "$HTML_FILE"' EXIT

# HTML_FILE is handed to Python via the environment, not interpolated into the
# heredoc as a string literal — mktemp's own output on this platform is plain
# alphanumeric (no quotes/backslashes to break out of a '...' literal), but a
# customized TMPDIR is outside this script's control, so the env var sidesteps
# the question entirely rather than trusting the shell metacharacter analysis.
export HTML_FILE
BODY=$("$PY" -c "
import os
from app.db import get_conn
from app.weekly import summary_text, summary_html, week_range
monday, friday = week_range()
with get_conn() as conn:
    text = summary_text(conn, monday, friday)
    html = summary_html(conn, monday, friday)
if text:
    with open(os.environ['HTML_FILE'], 'w', encoding='utf-8') as f:
        f.write(html)
    print(text)
")

# Same sender discipline as the brief, and for the same reason: mail-filing
# services commonly sort by SENDER and re-learn from where a message lands, so
# an informational message sharing a sender with the alert stream gets dragged
# into the alert folder no matter what the recipient says.
SUMMARY_ADDR="${SUMMARY_ADDR:-${BRIEF_ADDR:-commute@travismetcalf.com}}"

if [ -n "$BODY" ]; then
  SUBJECT="Commute week — $(TZ=America/New_York date '+%b %-d')"
  if RESULT=$(printf '%s\n' "$BODY" | "$PY" -m app.notify_cli weekly_summary \
        --subject "$SUBJECT" --from "$SUMMARY_ADDR" --to "$SUMMARY_ADDR" \
        --html-file "$HTML_FILE")
  then
    touch "$PROJECT_DIR/scripts/weekly-last-success"
    STATUS="$RESULT"
  else
    STATUS="send-failed ${RESULT:-}"
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') weekly $STATUS"
else
  # Heartbeat on a quiet week too: the marker means "the job RAN", not "there
  # was something to send" — this job fires ONCE a week, not five times like
  # brief.sh, so it gets exactly one chance a week to refresh the marker. Two
  # genuinely quiet weeks back to back (a holiday, a WFH stretch) would
  # otherwise leave it untouched for 14 days, cross the 691200s (8-day)
  # dead-man threshold, and page on a job that ran correctly both times — a
  # false page on healthy infrastructure. Same reasoning as poll.sh's
  # window-skip touch, and as notify_cli's fully-disabled exit 0: "correctly
  # did nothing" still counts as having run.
  touch "$PROJECT_DIR/scripts/weekly-last-success"
  echo "$(date '+%Y-%m-%d %H:%M:%S') weekly nothing to report"
fi
