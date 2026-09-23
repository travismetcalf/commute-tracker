#!/usr/bin/env python3
"""One poll cycle: fetch live travel times for every due route, write samples.

Run by poll.sh (launchd, every 10 min, window-gated there). Both directions and
all alternatives land in ONE transaction — a partial cycle rolls back entirely
rather than leaving half a poll; the next run is 10 minutes away.

Budget: reads settings.monthly_call_cap and exits 0 (rc meaning "nothing to do")
if the cycle would exceed it. A quota problem must degrade to "no new data",
never to a blocked key or a surprise bill.

Cycle counting for poll_divisor uses minutes-since-midnight // 10 so that a
divisor-3 route polls on cycles 0,3,6,... regardless of process restarts.
"""

import json
import os
import sys
from datetime import datetime, timezone

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from app import tomtom
from app.db import get_conn, get_routes, local_fields, utcnow, get_setting, LOCAL_TZ


def month_key() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m")


def upsert_geometry(conn, route_id: int, points: list, now: str) -> int | None:
    if not points:
        return None
    h = tomtom.geom_hash(points)
    conn.execute(
        """INSERT INTO route_geometry
             (route_id, geom_hash, points_json, bbox_json, length_meters,
              first_seen_utc, last_seen_utc)
           VALUES (?, ?, ?, ?, NULL, ?, ?)
           ON CONFLICT(route_id, geom_hash) DO UPDATE SET last_seen_utc=excluded.last_seen_utc""",
        (route_id, h, json.dumps(points, separators=(",", ":")),
         json.dumps(tomtom.bbox(points)), now, now),
    )
    row = conn.execute(
        "SELECT id FROM route_geometry WHERE route_id=? AND geom_hash=?", (route_id, h)
    ).fetchone()
    return row["id"] if row else None


def insert_sample(conn, route_id: int, kind: str, alt_index: int, route: dict,
                  now_utc_dt: datetime, geometry_id: int | None):
    s = tomtom.extract_summary(route)
    lf = local_fields(now_utc_dt)
    conn.execute(
        """INSERT INTO predicted_samples
             (route_id, ts_utc, local_date, local_hour, local_mod, local_dow, kind,
              alt_index, travel_sec, free_flow_sec, historic_sec, delay_sec,
              length_meters, geometry_id, sections_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (route_id, now_utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
         lf["local_date"], lf["local_hour"], lf["local_mod"], lf["local_dow"], kind,
         alt_index, s["travel_sec"], s["free_flow_sec"], s["historic_sec"],
         s["delay_sec"], s["length_meters"], geometry_id,
         json.dumps(tomtom.extract_sections(route), separators=(",", ":"))),
    )


def main() -> int:
    api_key = os.environ.get("TOMTOM_API_KEY", "")
    if not api_key:
        print("ERROR: TOMTOM_API_KEY not set", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    cycle = (datetime.now(LOCAL_TZ).hour * 60 + datetime.now(LOCAL_TZ).minute) // 10

    with get_conn() as conn:
        routes = get_routes(conn)
        places = {p["key"]: p for p in
                  (dict(r) for r in conn.execute("SELECT * FROM places"))}
        due = [r for r in routes if cycle % max(1, r["poll_divisor"]) == 0]
        if not due:
            print("no routes due this cycle")
            return 0

        cap = int(get_setting(conn, "monthly_call_cap", "18000"))
        row = conn.execute("SELECT calls FROM api_usage WHERE month=?", (month_key(),)).fetchone()
        used = row["calls"] if row else 0
        if used + len(due) > cap:
            print(f"WARNING: monthly call cap would be exceeded ({used}+{len(due)} > {cap}); skipping cycle")
            return 0

        n_samples = 0
        for r in due:
            o, d = places.get(r["origin_key"]), places.get(r["dest_key"])
            if not o or not d:
                print(f"WARNING: route {r['slug']} references missing place; skipping", file=sys.stderr)
                continue
            body = tomtom.calculate_route(
                api_key, (o["lat"], o["lon"]), (d["lat"], d["lon"]),
                via=json.loads(r["via_json"]) if r["via_json"] else None,
                max_alternatives=r["max_alternatives"],
            )
            for alt_index, route in enumerate(body.get("routes", [])):
                geom_id = upsert_geometry(conn, r["id"], tomtom.extract_points(route), utcnow())
                insert_sample(conn, r["id"], "live", alt_index, route, now, geom_id)
                n_samples += 1

        conn.execute(
            """INSERT INTO api_usage (month, calls, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(month) DO UPDATE SET calls=calls+excluded.calls,
                                                updated_at=excluded.updated_at""",
            (month_key(), len(due), utcnow()),
        )
        print(f"cycle {cycle}: {len(due)} calls, {n_samples} samples")
    return 0


if __name__ == "__main__":
    sys.exit(main())
