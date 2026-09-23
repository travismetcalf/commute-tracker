from app import notify
from app.db import set_setting


def test_defaults_apply_with_no_row(conn):
    assert notify.enabled(conn, "weekly_summary", notify.EMAIL) is True
    assert notify.enabled(conn, "brief", notify.EMAIL) is False
    assert notify.enabled(conn, "anomaly", notify.PUSH) is True
    assert notify.enabled(conn, "anomaly", notify.EMAIL) is False


def test_a_stored_value_overrides_the_default(conn):
    set_setting(conn, "notify_brief_email", "1")
    assert notify.enabled(conn, "brief", notify.EMAIL) is True
    set_setting(conn, "notify_brief_email", "0")
    assert notify.enabled(conn, "brief", notify.EMAIL) is False


def test_unsupported_channel_is_false_not_an_error(conn):
    """A caller asking the wrong question gets silence, not a 500."""
    assert notify.enabled(conn, "trip_completed", notify.EMAIL) is False
    assert notify.enabled(conn, "system", notify.PUSH) is False


def test_unknown_type_is_false(conn):
    assert notify.enabled(conn, "no_such_type", notify.EMAIL) is False


def test_a_stored_value_cannot_enable_an_unsupported_channel(conn):
    """Even a hand-written settings row must not conjure a channel the sender
    cannot reach."""
    set_setting(conn, "notify_trip_completed_email", "1")
    assert notify.enabled(conn, "trip_completed", notify.EMAIL) is False


def test_payload_shape(conn):
    p = {row["key"]: row for row in notify.payload(conn)}
    assert p["trip_completed"]["channels"] == ["push"]
    trip_completed_enabled = {s["channel"]: s["enabled"]
                              for s in p["trip_completed"]["switches"]}
    assert trip_completed_enabled == {"push": True}
    brief_enabled = {s["channel"]: s["enabled"] for s in p["brief"]["switches"]}
    assert brief_enabled == {"email": False, "push": False}
    assert p["weekly_summary"]["label"] == "Weekly summary"
    assert "Saturday" in p["weekly_summary"]["help"]
