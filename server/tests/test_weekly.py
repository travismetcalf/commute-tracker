from datetime import datetime, timedelta

from app import weekly
from app.db import LOCAL_TZ, local_fields, utcnow
from app.derive import derive

from tests.trip_fixtures import add_trip, seed_topology

SAT = datetime(2026, 9, 19, 10, 54, tzinfo=LOCAL_TZ)   # a Saturday


def _epoch(day: str, mod: int) -> int:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    return int((d + timedelta(minutes=mod)).timestamp())


def _add_directionless_trip(conn, day: str, depart_mod: int, duration_sec: int) -> str:
    """A trip whose direction never resolved: both ends carry no place, so
    derive.py's rule 8 else-branch fires (cands == []), route_id stays NULL,
    and the trip is flagged needs_review, reason='direction unresolved'.

    trip_fixtures.add_trip cannot build this shape -- it always supplies a
    known origin (HOME or OFFICE) for the opening geofence -- so this goes
    directly against trip_events, the same append-only log derive() reads,
    using placeless start/end kinds (drive_start/drive_end) instead of
    geofence crossings. Returns the start_event_key so the caller can assert
    on the resulting trip and then have a human confirm it.
    """
    start_ts = _epoch(day, depart_mod)
    end_ts = start_ts + duration_sec
    start_key = f"{day}-{depart_mod}-directionless-start"
    end_key = f"{day}-{depart_mod}-directionless-end"
    for key, kind, ts in ((start_key, "drive_start", start_ts),
                          (end_key, "drive_end", end_ts)):
        lf = local_fields(datetime.fromtimestamp(ts, tz=LOCAL_TZ))
        conn.execute(
            """INSERT INTO trip_events
                 (event_key, kind, place_key, source, confidence, device_ts,
                  server_ts, local_date, local_mod, local_dow, raw_json)
               VALUES (?, ?, NULL, 'test', NULL, ?, ?, ?, ?, ?, '{}')""",
            (key, kind, ts, utcnow(),
             lf["local_date"], lf["local_mod"], lf["local_dow"]),
        )
    return start_key


def test_week_range_is_the_monday_to_friday_just_ending():
    monday, friday = weekly.week_range(now=SAT)
    assert monday == "2026-09-14"
    assert friday == "2026-09-18"


def test_empty_week_returns_none(conn):
    seed_topology(conn)
    conn.commit()
    assert weekly.summary_text(conn, *weekly.week_range(now=SAT)) is None


def test_totals_and_per_direction_reconcile(conn):
    """The measured hazard: an inner JOIN routes drops a complete trip whose
    direction never resolved, so the headline count and the per-direction
    blocks stop adding up with no error anywhere.

    The fourth trip is built directly against trip_events (see
    _add_directionless_trip) precisely so an inner join has a row to drop:
    both ends are placeless, so derive.py's rule 8 leaves route_id NULL and
    flags it 'direction unresolved' -- and a human then confirms it, which is
    what makes COUNTS_TOWARD_AVERAGES admit it despite the missing route.
    """
    seed_topology(conn)
    add_trip(conn, day="2026-09-14", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound")
    add_trip(conn, day="2026-09-14", depart_mod=17 * 60, duration_sec=3000,
             heading="inbound")
    add_trip(conn, day="2026-09-15", depart_mod=8 * 60, duration_sec=2100,
             heading="outbound")
    start_key = _add_directionless_trip(conn, day="2026-09-16",
                                         depart_mod=8 * 60, duration_sec=6480)
    conn.commit()
    derive()

    row = conn.execute(
        "SELECT status, route_id FROM trips WHERE start_event_key=?",
        (start_key,)).fetchone()
    assert row["status"] == "complete" and row["route_id"] is None, (
        "fixture invalid: expected a complete, direction-unresolved trip -- "
        "the test below cannot exercise the join hazard without one")
    conn.execute("UPDATE trips SET verdict='confirmed' WHERE start_event_key=?",
                 (start_key,))
    conn.commit()

    data = weekly.gather(conn, *weekly.week_range(now=SAT))
    # These three are the hazard guard: an inner JOIN routes drops the
    # directionless row from the query entirely, before gather() ever buckets
    # it, so these hardcoded expectations are what would go red.
    assert data["n"] == 4                      # 3 directional + 1 directionless
    assert data["total_sec"] == 2400 + 3000 + 2100 + 6480
    assert "unknown" in data["directions"]      # the directionless bucket exists

    # This reconciliation is a tautology of gather()'s Python-side bucketing,
    # NOT a join-hazard guard: `directions` buckets every row already present
    # in `rows` exactly once, so per_dir and total_sec are two sums over the
    # same in-memory list and will always agree with EACH OTHER even when a
    # bad SQL join has already dropped a row from `rows` before bucketing
    # ever runs. What it does catch is a bucketing bug -- a row silently
    # excluded from `directions` while still counted in the total.
    per_dir = sum(d["total_sec"] for d in data["directions"].values())
    assert per_dir == data["total_sec"]


def test_best_and_worst_are_per_direction(conn):
    seed_topology(conn)
    add_trip(conn, day="2026-09-14", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound")
    add_trip(conn, day="2026-09-15", depart_mod=8 * 60, duration_sec=2100,
             heading="outbound")
    add_trip(conn, day="2026-09-14", depart_mod=17 * 60, duration_sec=3600,
             heading="inbound")
    conn.commit()
    derive()
    data = weekly.gather(conn, *weekly.week_range(now=SAT))
    out = data["directions"]["outbound"]
    assert out["best"]["duration_sec"] == 2100
    assert out["worst"]["duration_sec"] == 2400
    assert "inbound" in data["directions"]


def test_day_by_day_has_five_rows_including_empty_days(conn):
    """A GROUP BY only returns days that have trips; a zero-trip Wednesday must
    still appear or the table silently loses a column."""
    seed_topology(conn)
    add_trip(conn, day="2026-09-14", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound")
    conn.commit()
    derive()
    data = weekly.gather(conn, *weekly.week_range(now=SAT))
    assert len(data["days"]) == 5
    assert [d["dow"] for d in data["days"]] == [0, 1, 2, 3, 4]
    assert data["days"][0]["n"] == 1
    assert data["days"][2]["n"] == 0


def test_weekend_trips_are_excluded(conn):
    seed_topology(conn)
    add_trip(conn, day="2026-09-14", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound")
    add_trip(conn, day="2026-09-13", depart_mod=8 * 60, duration_sec=9999,
             heading="outbound")      # the Sunday before
    conn.commit()
    derive()
    data = weekly.gather(conn, *weekly.week_range(now=SAT))
    assert data["n"] == 1


def test_text_names_the_dates_it_covers(conn):
    seed_topology(conn)
    add_trip(conn, day="2026-09-14", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound")
    conn.commit()
    derive()
    text = weekly.summary_text(conn, *weekly.week_range(now=SAT))
    assert "Sep 14" in text and "Sep 18" in text


def test_esc_neutralises_markup():
    """_esc is a real function with real behaviour and deserves direct
    coverage rather than a rendering test that also passes when the value it
    claims to protect is never rendered at all (see weekly.py's _esc
    docstring -- nothing here is user-controlled today, which is exactly why
    the escaping itself, not any one call site, is what must be verified)."""
    assert weekly._esc('Acme <script>x</script>') == \
        'Acme &lt;script&gt;x&lt;/script&gt;'
    assert weekly._esc('a "b" & c') == 'a &quot;b&quot; &amp; c'


def test_html_and_text_report_the_same_totals(conn):
    seed_topology(conn)
    add_trip(conn, day="2026-09-14", depart_mod=8 * 60, duration_sec=2400,
             heading="outbound")
    conn.commit()
    derive()
    rng = weekly.week_range(now=SAT)
    text = weekly.summary_text(conn, *rng)
    html = weekly.summary_html(conn, *rng)
    assert "40 min" in text
    assert "40 min" in html
    assert html.lstrip().startswith("<")
