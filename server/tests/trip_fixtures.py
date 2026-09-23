"""Build the topology and events that trips derive from.

trips.start_event_key is a foreign key into trip_events and get_conn sets
PRAGMA foreign_keys=ON, so a test cannot simply INSERT a trips row — it has to
write the events and let derive() pair them. That is also the honest thing to
test against: it exercises the real pairing rules rather than a hand-made row
that derive would never have produced.

Coordinates here are deliberately low-precision placeholders. A release gate
(scripts/publish-oss.sh) hard-aborts on any lat/lon-precision literal, so these
must stay coarse.
"""

from datetime import datetime, timedelta

from app.db import LOCAL_TZ, local_fields, utcnow

HOME = "home"
OFFICE = "office"


def seed_topology(conn):
    """One home, one office, and the two routes between them."""
    conn.executemany(
        """INSERT OR IGNORE INTO places
             (key, label, kind, lat, lon, geofence_radius_m, monitored)
           VALUES (?, ?, ?, ?, ?, 175, 1)""",
        [(HOME, "Home", "home", 40.0, -75.0),
         (OFFICE, "Test Office", "office", 40.1, -75.1)],
    )
    conn.executemany(
        """INSERT OR IGNORE INTO routes
             (slug, label, origin_key, dest_key, heading, role,
              poll_divisor, max_alternatives, enabled)
           VALUES (?, ?, ?, ?, ?, 'primary', 1, 0, 1)""",
        [("home-to-office", "Home to Test Office", HOME, OFFICE, "outbound"),
         ("office-to-home", "Test Office to Home", OFFICE, HOME, "inbound")],
    )


def _epoch(day: str, mod: int) -> int:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    return int((d + timedelta(minutes=mod)).timestamp())


def _event(conn, key, kind, place_key, ts):
    lf = local_fields(datetime.fromtimestamp(ts, tz=LOCAL_TZ))
    conn.execute(
        """INSERT OR IGNORE INTO trip_events
             (event_key, kind, place_key, source, confidence, device_ts,
              server_ts, local_date, local_mod, local_dow, raw_json)
           VALUES (?, ?, ?, 'test', 1.0, ?, ?, ?, ?, ?, '{}')""",
        (key, kind, place_key, ts, utcnow(),
         lf["local_date"], lf["local_mod"], lf["local_dow"]),
    )


def add_trip(conn, day: str, depart_mod: int, duration_sec: int,
             heading: str, needs_review: bool = False) -> str:
    """Write the geofence pair for one trip and return its start_event_key.

    needs_review=True drops only the DESTINATION place from the closing
    crossing. derive then takes rule 8 (derive.py:255-260): exactly one route
    leaves the known origin, so it infers the destination, sets route_id, and
    flags needs_review=1 with reason 'destination inferred'.

    Dropping BOTH places instead would reach 'direction unresolved' and leave
    route_id NULL — which is a worse fixture for testing an averages predicate,
    because a NULL route_id is filtered out by the route match anyway and the
    test would pass whether or not the predicate was applied.
    """
    origin, dest = (HOME, OFFICE) if heading == "outbound" else (OFFICE, HOME)
    start_ts = _epoch(day, depart_mod)
    end_ts = start_ts + duration_sec
    # heading is part of the key: two trips on the same day at the same
    # depart_mod but opposite directions must not collide. Without it,
    # INSERT OR IGNORE silently drops the second call's events with no error.
    start_key = f"{day}-{depart_mod}-{heading}-leave"
    _event(conn, start_key, "geofence_leave", origin, start_ts)
    _event(conn, f"{day}-{depart_mod}-{heading}-enter", "geofence_enter",
           None if needs_review else dest, end_ts)
    return start_key
