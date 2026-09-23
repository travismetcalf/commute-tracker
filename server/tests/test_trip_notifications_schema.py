import sqlite3

import pytest

from app.derive import derive

from tests.trip_fixtures import add_trip, seed_topology


def test_table_exists_with_the_expected_columns(conn):
    cols = {r["name"]: r for r in conn.execute(
        "PRAGMA table_info(trip_notifications)")}
    assert set(cols) == {"start_event_key", "considered_at", "sent_at", "verdict"}
    assert cols["start_event_key"]["pk"] == 1
    assert cols["considered_at"]["notnull"] == 1


def test_a_second_row_for_the_same_trip_is_rejected(conn):
    conn.execute(
        """INSERT INTO trip_notifications (start_event_key, considered_at)
           VALUES ('k', 'now')""")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO trip_notifications (start_event_key, considered_at)
               VALUES ('k', 'later')""")


def test_rows_survive_a_full_rebuild(conn):
    """--rebuild-all DELETEs every trip. If the marker lived on the trips row it
    would go with them and the whole history would re-notify."""
    seed_topology(conn)
    key = add_trip(conn, day="2026-09-14", depart_mod=8 * 60,
                   duration_sec=2400, heading="outbound")
    conn.commit()
    derive()
    before_id = conn.execute(
        "SELECT id FROM trips WHERE start_event_key=?", (key,)).fetchone()["id"]
    conn.execute(
        """INSERT INTO trip_notifications (start_event_key, considered_at, verdict)
           VALUES (?, 'now', 'sent')""", (key,))
    conn.commit()

    derive(rebuild=True)

    assert conn.execute("SELECT COUNT(*) AS n FROM trips").fetchone()["n"] == 1
    after_id = conn.execute(
        "SELECT id FROM trips WHERE start_event_key=?", (key,)).fetchone()["id"]
    # The observable signature of a real DELETE-and-reinsert, not a no-op: the
    # AUTOINCREMENT id changes even though the trip is otherwise identical.
    assert after_id != before_id
    row = conn.execute(
        "SELECT * FROM trip_notifications WHERE start_event_key=?",
        (key,)).fetchone()
    assert row is not None
    assert row["verdict"] == "sent"
