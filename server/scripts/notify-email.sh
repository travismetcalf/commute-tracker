#!/usr/bin/env bash
# Email notification hook. If a site notifier is configured (NOTIFY_CMD in
# .env, or a notifier script at the path below), failures/alerts go through
# it; otherwise notifications no-op so every caller keeps working without
# mail infrastructure. Callers: poll.sh, brief.sh (and operator-only jobs).
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$(dirname "$SCRIPT_DIR")/.env"
NOTIFY_CMD="$(grep '^NOTIFY_CMD=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
NOTIFY_ENV_FILE="$(grep '^NOTIFY_ENV_FILE=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
CANONICAL="$HOME/projects/_shared/scripts/send-email.sh"

if [ -n "$NOTIFY_CMD" ]; then
  exec $NOTIFY_CMD "$@"
elif [ -x "$CANONICAL" ]; then
  exec env ${NOTIFY_ENV_FILE:+NOTIFY_ENV_FILE="$NOTIFY_ENV_FILE"} "$CANONICAL" "$@"
else
  echo "[notify] no notifier configured — skipping: $*" >&2
  exit 0
fi
