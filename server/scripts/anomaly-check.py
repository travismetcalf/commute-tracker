#!/usr/bin/env python3
"""Anomaly check, run by poll.sh after each successful cycle. One alert per
route per day, tracked in scripts/anomaly-state (gitignored)."""

import json
import os
import sys
import traceback
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from app.brief import check_anomaly  # noqa: E402
from app.db import LOCAL_TZ, get_conn  # noqa: E402
from app import notify, push  # noqa: E402

STATE = os.path.join(PROJECT_DIR, "scripts", "anomaly-state")


def main() -> int:
    # Every successful poll cycle = fresh data → nudge the widget. A silent
    # push is free (priority 5, no user-visible anything) and turns the
    # widget's 15–60 min staleness into ~10 min during the polling window.
    push.send_silent()

    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}

    alerts = [a for a in check_anomaly() if state.get(a["route_slug"]) != today]
    if not alerts:
        return 0

    body_lines = ["Commute running well above normal:"]
    for a in alerts:
        body_lines.append(
            f"  {a['route_slug']}: {a['sample_min']} min now "
            f"(median {a['median_min']}, p90 {a['p90_min']})"
        )
    body = "\n".join(body_lines)

    # One call, both channels, each gated. This used to be an unconditional
    # push followed by an unconditional email — the same alert arriving twice.
    #
    # notify.send() RETURNING is a considered outcome, even a "failed" or
    # "unavailable" verdict — the dedupe slot below is written for it, same as
    # before, so a route already flagged today does not re-alert next cycle.
    # As of 2026-09-18 that includes a push-delivery fault: notify.send()
    # itself now catches whatever push.send_alert() raises (a bad/truncated
    # .p8 out of push._bearer(), a locked database out of push._send()'s own
    # get_conn()) and reports "failed" for that channel instead of letting it
    # propagate — email, sent first for this type, is unaffected. So the
    # ordinary "push is broken" case no longer reaches this except clause.
    # notify.send() RAISING is still possible for what is a genuine
    # programming error rather than a delivery fault — an unknown type_key
    # (KeyError) or an unhandled channel (ValueError) — and get_conn() on the
    # next line can still raise on its own account (e.g. this connection
    # itself hitting a lock timeout, before notify.send() is even called).
    # Writing the dedupe slot for either would permanently silence a real
    # anomaly for the rest of the day with no record anywhere, since poll.sh
    # backgrounds this whole script with `|| true`. So a raise must leave
    # `state` untouched (retryable next cycle) and must be visible on
    # stdout — that is the one stream poll.sh no longer discards, but print
    # it explicitly anyway rather than relying on that alone.
    try:
        with get_conn() as conn:
            result = notify.send(conn, "anomaly", "Commute alert", body)
    except Exception:
        print("ANOMALY DISPATCH FAILED — not marked as delivered, will retry "
              "next cycle:")
        print(traceback.format_exc())
        return 1

    print(" ".join(f"{k}={v}" for k, v in sorted(result.items())))
    for a in alerts:
        state[a["route_slug"]] = today
    json.dump(state, open(STATE, "w"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
