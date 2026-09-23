def test_fixture_gives_a_schema_and_seeded_defaults(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"trips", "trip_events", "settings", "devices"} <= tables
    assert conn.execute(
        "SELECT value FROM settings WHERE key='monthly_call_cap'"
    ).fetchone()["value"] == "18000"


def test_each_test_gets_a_clean_database(conn):
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('probe', 'x', '')")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM settings WHERE key='probe'").fetchone()["n"] == 1
