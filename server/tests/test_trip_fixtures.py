from app.derive import derive

from tests.trip_fixtures import add_trip, seed_topology


def test_seed_topology_creates_a_home_an_office_and_both_routes(conn):
    seed_topology(conn)
    kinds = {r["key"]: r["kind"] for r in conn.execute("SELECT key, kind FROM places")}
    assert kinds == {"home": "home", "office": "office"}
    headings = {r["heading"] for r in conn.execute("SELECT heading FROM routes")}
    assert headings == {"outbound", "inbound"}


def test_add_trip_produces_a_derivable_pair(conn):
    seed_topology(conn)
    add_trip(conn, day="2026-09-14", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound")
    conn.commit()
    stats = derive()
    assert stats["complete"] == 1
    row = conn.execute("SELECT * FROM trips").fetchone()
    assert row["status"] == "complete"
    assert row["duration_sec"] == 2400
    assert row["local_dow"] == 0        # 2026-09-14 is a Monday
    assert row["local_mod"] == 8 * 60


def test_trips_rows_satisfy_the_foreign_key(conn):
    """trips.start_event_key REFERENCES trip_events(event_key) with
    foreign_keys=ON, so a fixture that skips the events raises."""
    seed_topology(conn)
    add_trip(conn, day="2026-09-15", depart_mod=17 * 60, duration_sec=3000,
             heading="inbound")
    conn.commit()
    derive()
    n = conn.execute(
        """SELECT COUNT(*) AS n FROM trips t
           JOIN trip_events e ON t.start_event_key = e.event_key""").fetchone()["n"]
    assert n == 1


def test_needs_review_trip_can_be_built(conn):
    seed_topology(conn)
    add_trip(conn, day="2026-09-16", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound", needs_review=True)
    conn.commit()
    derive()
    row = conn.execute("SELECT * FROM trips").fetchone()
    assert row["needs_review"] == 1
    assert row["route_id"] is not None
    assert row["review_reason"] == "destination inferred"


def test_same_day_same_time_opposite_directions_both_exist(conn):
    """Two add_trip calls sharing (day, depart_mod) but differing only in
    heading must not collide on event_key. Before this fix, the key ignored
    heading, so the second call's INSERT OR IGNORE silently dropped both of
    its events and this table ended up with 2 rows instead of 4 — the
    inbound trip never existed.

    This does not call derive(): both calls use the same depart_mod, so their
    opening geofence_leave events land at the identical device_ts (leaving
    home and leaving the office at the same instant isn't something one
    traveler does). derive.py's pairing engine (rules 1-3) is a single-open
    -trip sequential state machine and, correctly and by design, can't open a
    second concurrent trip — it keeps whichever leave was inserted first and
    discards the other's leave, orphaning its matching enter. That's real,
    unmodified derive.py behaviour for a degenerate concurrent input, not
    something this fixture-level test should assert an outcome for. The
    property being guarded here is the one Finding 1 is about: the four
    underlying trip_events rows all exist, are distinct, and keep each
    direction's own places.
    """
    seed_topology(conn)
    add_trip(conn, day="2026-09-14", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound")
    add_trip(conn, day="2026-09-14", depart_mod=8 * 60, duration_sec=3000,
             heading="inbound")
    conn.commit()
    rows = conn.execute(
        "SELECT event_key, kind, place_key FROM trip_events").fetchall()
    assert len(rows) == 4
    assert len({r["event_key"] for r in rows}) == 4  # no collision

    places_by_kind = {}
    for r in rows:
        places_by_kind.setdefault(r["kind"], set()).add(r["place_key"])
    assert places_by_kind["geofence_leave"] == {"home", "office"}
    assert places_by_kind["geofence_enter"] == {"home", "office"}
