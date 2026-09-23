#!/usr/bin/env python3
"""One-time (re-runnable) historical backfill via TomTom's departAt.

Setting departAt to a FUTURE time disables live traffic and returns durations
from TomTom's historical speed profiles (per road, per weekday, 5-min bins) —
so the hour-of-day / weekday charts are populated on day one instead of after
two months of polling.

Coverage: next Mon-Fri, 07:00-19:00 ET at 15-min bins, every enabled route.
~8 routes x 5 days x 48 bins = ~1,920 calls, throttled well under TomTom's
~5 QPS / 2,500-per-day limits (default pace ~2 QPS; use --max-calls to split
across days if route count grows).

Idempotent: historical rows for each (route, dow, mod) are DELETED and
rewritten, never accumulated. Live rows are untouched.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
# Runs directly on the host (no wrapper .sh) — default DB_PATH to the project's
# data dir instead of app.db's in-container default.
os.environ.setdefault("DB_PATH", os.path.join(PROJECT_DIR, "data", "commute.db"))

from app import tomtom
from app.db import get_conn, get_routes, utcnow, LOCAL_TZ

WINDOW_START_H, WINDOW_END_H = 7, 19
BIN_MIN = 15
PACE_SEC = 0.5  # ~2 QPS


def next_weekday_dates() -> list[datetime]:
    """The next Mon..Fri as local dates (order doesn't matter — rows key on dow)."""
    today = datetime.now(LOCAL_TZ).date()
    out = {}
    d = today + timedelta(days=1)
    while len(out) < 5:
        if d.weekday() < 5:
            out[d.weekday()] = d
        d += timedelta(days=1)
    return [datetime(x.year, x.month, x.day, tzinfo=LOCAL_TZ) for x in out.values()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-calls", type=int, default=2400,
                    help="stop after N calls (stay under TomTom's daily cap)")
    args = ap.parse_args()

    api_key = os.environ.get("TOMTOM_API_KEY", "")
    if not api_key:
        # Host-script convenience: read straight from .env like the other scripts.
        env_path = os.path.join(PROJECT_DIR, ".env")
        if os.path.exists(env_path):
            for line in open(env_path):
                if line.startswith("TOMTOM_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
    if not api_key:
        print("ERROR: TOMTOM_API_KEY not set", file=sys.stderr)
        return 1

    # Read config in one short connection; NEVER hold a write transaction across
    # API calls — the run takes ~15 min and a long write txn starves the live
    # poller (one writer at a time in WAL). API results buffer in memory and
    # each (route, day) commits in its own short transaction, so progress is
    # durable per-day and a crash loses at most 48 calls.
    with get_conn() as conn:
        routes = get_routes(conn)
        places = {p["key"]: dict(p) for p in conn.execute("SELECT * FROM places")}
    if not routes or not places:
        print("ERROR: seed places and routes first (see /settings)", file=sys.stderr)
        return 1

    calls = 0

    def commit_day(route_id: int, rows: list):
        with get_conn() as conn:
            for dow, slot, values in rows:
                conn.execute(
                    """DELETE FROM predicted_samples
                       WHERE route_id=? AND kind='historical' AND local_dow=? AND local_mod=?""",
                    (route_id, dow, slot),
                )
                conn.execute(
                    """INSERT INTO predicted_samples
                         (route_id, ts_utc, local_date, local_hour, local_mod,
                          local_dow, kind, alt_index, travel_sec, free_flow_sec,
                          historic_sec, delay_sec, length_meters, geometry_id, sections_json)
                       VALUES (?, ?, ?, ?, ?, ?, 'historical', 0, ?, ?, ?, ?, ?, NULL, NULL)""",
                    values,
                )

    def commit_usage(n: int):
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO api_usage (month, calls, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(month) DO UPDATE SET calls=calls+excluded.calls,
                                                    updated_at=excluded.updated_at""",
                (datetime.now(LOCAL_TZ).strftime("%Y-%m"), n, utcnow()),
            )

    for r in routes:
        o, d = places[r["origin_key"]], places[r["dest_key"]]
        via = json.loads(r["via_json"]) if r["via_json"] else None
        for day in next_weekday_dates():
            day_rows, day_calls = [], 0
            for slot in range(WINDOW_START_H * 60, WINDOW_END_H * 60, BIN_MIN):
                if calls >= args.max_calls:
                    commit_day(r["id"], day_rows)
                    commit_usage(day_calls)
                    print(f"stopping at --max-calls={args.max_calls}; rerun to continue")
                    return 0
                depart = day.replace(hour=slot // 60, minute=slot % 60)
                body = tomtom.calculate_route(
                    api_key, (o["lat"], o["lon"]), (d["lat"], d["lon"]),
                    via=via, depart_at=depart.isoformat(),
                )
                calls += 1
                day_calls += 1
                route0 = (body.get("routes") or [None])[0]
                if route0 is None:
                    continue
                s = tomtom.extract_summary(route0)
                day_rows.append((depart.weekday(), slot,
                    (r["id"], utcnow(), depart.strftime("%Y-%m-%d"), slot // 60,
                     slot, depart.weekday(), s["travel_sec"], s["free_flow_sec"],
                     s["historic_sec"], s["delay_sec"], s["length_meters"])))
                time.sleep(PACE_SEC)
            commit_day(r["id"], day_rows)
            commit_usage(day_calls)
            print(f"route {r['slug']} {day.strftime('%a')}: {day_calls} calls committed", flush=True)

    print(f"backfill complete: {calls} calls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
