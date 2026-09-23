from app.derive import derive
from app.stats import DEPART_WINDOW_MIN, trip_bucket

from tests.trip_fixtures import add_trip, seed_topology


def _route_id(conn, heading):
    return conn.execute(
        "SELECT id FROM routes WHERE heading=?", (heading,)).fetchone()["id"]


def test_empty_bucket_reports_zero_samples(conn):
    seed_topology(conn)
    conn.commit()
    b = trip_bucket(conn, _route_id(conn, "outbound"), 8 * 60)
    assert b["n"] == 0
    assert b["avg_sec"] is None


def test_averages_trips_in_the_same_window(conn):
    seed_topology(conn)
    for day, dur in [("2026-09-14", 2400), ("2026-09-15", 2700),
                     ("2026-09-16", 2100)]:
        add_trip(conn, day=day, depart_mod=8 * 60, duration_sec=dur,
                 heading="outbound")
    conn.commit()
    derive()
    b = trip_bucket(conn, _route_id(conn, "outbound"), 8 * 60)
    assert b["n"] == 3
    assert b["avg_sec"] == 2400


def test_window_is_plus_or_minus_depart_window_min(conn):
    """A trip exactly at the edge is in; one minute past it is out."""
    seed_topology(conn)
    centre = 8 * 60
    add_trip(conn, day="2026-09-14", depart_mod=centre - DEPART_WINDOW_MIN,
             duration_sec=1800, heading="outbound")
    add_trip(conn, day="2026-09-15", depart_mod=centre + DEPART_WINDOW_MIN,
             duration_sec=1800, heading="outbound")
    add_trip(conn, day="2026-09-16", depart_mod=centre + DEPART_WINDOW_MIN + 1,
             duration_sec=9999, heading="outbound")
    conn.commit()
    derive()
    b = trip_bucket(conn, _route_id(conn, "outbound"), centre)
    assert b["n"] == 2
    assert b["avg_sec"] == 1800


def test_other_routes_are_excluded(conn):
    """The two trips MUST land on different DAYS but the SAME depart_mod.

    Different day: derive is a single-open-trip sequential state machine —
    two geofence_leave events at the identical device_ts cannot both open a
    trip, the second is discarded and its matching enter is later dropped as
    an orphan. Measured: same day AND same depart_mod writes 4 events but
    derives only 1 trip. A different day gives the inbound trip its own
    device_ts so both trips actually exist.

    Same depart_mod (8:00, not 17:00): the departure-window filter is
    ±DEPART_WINDOW_MIN around the query centre. A trip outside that window is
    already excluded before the route filter is ever reached, so putting the
    inbound trip at 17:00 would make this test pass even with
    `AND t.route_id = ?` deleted from trip_bucket's query — it would only be
    re-proving test_window_is_plus_or_minus_depart_window_min. Keeping both
    trips at 8:00 is what forces the route filter to be the thing doing the
    exclusion.
    """
    seed_topology(conn)
    add_trip(conn, day="2026-09-14", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound")
    add_trip(conn, day="2026-09-15", depart_mod=8 * 60, duration_sec=9999,
             heading="inbound")
    conn.commit()
    derive()

    # Guard the guard: prove BOTH trips exist before asserting one is filtered.
    # An empty or short result is exactly what a fixture that silently built
    # nothing would also produce.
    assert conn.execute("SELECT COUNT(*) AS n FROM trips").fetchone()["n"] == 2

    b = trip_bucket(conn, _route_id(conn, "outbound"), 8 * 60)
    assert b["n"] == 1
    assert b["avg_sec"] == 2400


def test_weekend_trips_are_excluded(conn):
    """Every aggregate in this module is weekday-only; this one matches."""
    seed_topology(conn)
    add_trip(conn, day="2026-09-14", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound")               # Monday
    add_trip(conn, day="2026-09-19", depart_mod=8 * 60, duration_sec=9999,
             heading="outbound")               # Saturday
    conn.commit()
    derive()
    b = trip_bucket(conn, _route_id(conn, "outbound"), 8 * 60)
    assert b["n"] == 1


def test_flagged_trips_are_excluded_by_the_shared_predicate(conn):
    """COUNTS_TOWARD_AVERAGES excludes an unadjudicated needs_review row.
    Reusing the predicate rather than re-spelling it is what guarantees this."""
    seed_topology(conn)
    add_trip(conn, day="2026-09-14", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound")
    add_trip(conn, day="2026-09-15", depart_mod=8 * 60, duration_sec=9999,
             heading="outbound", needs_review=True)
    conn.commit()
    derive()

    # Guard the guard: this test is only meaningful if the flagged trip DID get
    # a route_id. If it were NULL the route match would exclude it regardless
    # and the test would pass whether or not the predicate was applied.
    flagged = conn.execute(
        "SELECT route_id, needs_review FROM trips WHERE duration_sec=9999"
    ).fetchone()
    assert flagged["needs_review"] == 1
    assert flagged["route_id"] == _route_id(conn, "outbound")

    b = trip_bucket(conn, _route_id(conn, "outbound"), 8 * 60)
    assert b["n"] == 1
    assert b["avg_sec"] == 2400


def test_exclude_key_drops_that_one_trip_from_the_pool(conn):
    """The optional exclude_key parameter removes exactly one trip — the
    caller comparing a trip against "the average" must not have that trip
    counted in the average it is compared against (see trip_notify.compose,
    which passes the trip's own start_event_key here)."""
    seed_topology(conn)
    key = add_trip(conn, day="2026-09-14", depart_mod=8 * 60,
                   duration_sec=3000, heading="outbound")
    add_trip(conn, day="2026-09-15", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound")
    add_trip(conn, day="2026-09-16", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound")
    conn.commit()
    derive()

    route_id = _route_id(conn, "outbound")
    b = trip_bucket(conn, route_id, 8 * 60)
    assert b["n"] == 3
    assert b["avg_sec"] == 2600

    b_excl = trip_bucket(conn, route_id, 8 * 60, exclude_key=key)
    assert b_excl["n"] == 2
    assert b_excl["avg_sec"] == 2400


def test_confirming_a_flagged_trip_admits_it(conn):
    """The other half of the predicate: `NOT (needs_review=1 AND verdict='auto')`
    means a human verdict pulls the row straight back in."""
    seed_topology(conn)
    add_trip(conn, day="2026-09-14", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound")
    add_trip(conn, day="2026-09-15", depart_mod=8 * 60, duration_sec=3000,
             heading="outbound", needs_review=True)
    conn.commit()
    derive()
    assert trip_bucket(conn, _route_id(conn, "outbound"), 8 * 60)["n"] == 1

    conn.execute(
        "UPDATE trips SET verdict='confirmed' WHERE duration_sec=3000")
    conn.commit()
    b = trip_bucket(conn, _route_id(conn, "outbound"), 8 * 60)
    assert b["n"] == 2
    assert b["avg_sec"] == 2700
