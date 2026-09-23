#!/usr/bin/env bash
# Re-derive trips from the append-only event log. Runs every 15 min via launchd
# and on demand. --rebuild-all truncates trips and replays the whole log.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export DB_PATH="$PROJECT_DIR/data/commute.db"

# On the mac there is no .venv, so this falls through to Homebrew python3 via
# the PATH export above — identical to pre-port behavior. On Linux
# install.sh provisions .venv with httpx (PEP 668 blocks a
# system-wide pip install there).
PY="$PROJECT_DIR/.venv/bin/python3"; [ -x "$PY" ] || PY="python3"

REBUILD="False"
[ "${1:-}" = "--rebuild-all" ] && REBUILD="True"

cd "$PROJECT_DIR"
"$PY" -c "
import sys; sys.path.insert(0, '$PROJECT_DIR')
from app.derive import derive
print('derive:', derive(rebuild=$REBUILD))
# A notify fault must not retract a successful derive's dead-man marker: the
# script deploy table ships scripts/* by git pull and app/* by container
# rebuild, so a notify-side schema drift between those two steps (e.g.
# trip_notifications not yet created) must not make a good derive look dead.
try:
    from app.trip_notify import notify_completed_trips
    for r in notify_completed_trips():
        print('notify:', r['start_event_key'], r['verdict'])
except Exception as exc:
    print(f'notify RAISED: {type(exc).__name__}: {exc}')
" && touch "$PROJECT_DIR/scripts/derive-last-success"
