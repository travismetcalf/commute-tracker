from app import brief as brief_mod
from app import main, notify


def test_every_notify_key_is_settable():
    for key in notify.defaults():
        assert key in main._SETTING_VALIDATORS, key


def test_notify_validators_reject_shell_bait():
    v = main._SETTING_VALIDATORS["notify_brief_email"]
    assert v("1")
    assert not v("1; rm -rf /")
    assert not v("$(id)")


def test_existing_validators_survive_the_merge():
    assert main._SETTING_VALIDATORS["monthly_call_cap"]("18000")
    assert not main._SETTING_VALIDATORS["monthly_call_cap"]("abc")
    assert main._SETTING_VALIDATORS["alert_days"]("0,1,2,3,4")


def test_config_exposes_the_catalog(conn, monkeypatch):
    """Must actually exercise api_config() — a test that only calls
    notify.payload() directly would still pass if main.py's own
    `"notifications": notify_mod.payload(conn)` line were misspelled,
    pointed at the wrong function, or deleted outright."""
    monkeypatch.setattr(main, "require_bearer", lambda request: None)
    result = main.api_config(None)
    assert "alerts" in result  # the shipped iOS app still reads this one
    assert result["notifications"] == notify.payload(conn)
    rows = {r["key"]: r for r in result["notifications"]}
    assert set(rows) == {"brief", "weekly_summary", "trip_completed",
                         "anomaly", "system"}


def test_api_settings_normalises_json_booleans():
    """A Swift client encodes a toggle as true/false, not "1"/"0"."""
    assert main._norm_setting_value(True) == "1"
    assert main._norm_setting_value(False) == "0"
    assert main._norm_setting_value("1") == "1"
    assert main._norm_setting_value(0) == "0"


def test_api_settings_allowlist_stays_in_sync_with_alert_defaults():
    """This is the exact drift mode the branch exists to eliminate: the read
    side (/api/v1/config's `alerts` payload) derives from ALERT_DEFAULTS, so
    the write side must too, or a new alert key gets advertised but silently
    dropped on write."""
    allowed = main._api_setting_validators()
    for key in brief_mod.ALERT_DEFAULTS:
        assert key in allowed, key


def test_api_settings_allowlist_covers_both_families():
    allowed = main._api_setting_validators()
    assert "alert_days" in allowed
    assert "notify_weekly_summary_email" in allowed
    assert allowed["notify_weekly_summary_email"]("1")
    assert not allowed["notify_weekly_summary_email"]("yes")
    assert allowed["alert_am_start"]("06:30")
    assert not allowed["alert_am_start"]("25:00")


def test_oversized_value_rejected():
    """The `len(v) > 64` bound in _apply_setting_updates guards the same
    settings table smoke.sh's shell arithmetic reads — an unbounded string
    reaching it is the RCE primitive main.py's own _SETTING_VALIDATORS
    comment warns about."""
    allowed = main._api_setting_validators()

    over = "0" * 65
    updates, bad = main._apply_setting_updates(
        {"alert_am_start": over, "alert_am_end": "06:30"}, allowed)
    assert "alert_am_start" in bad
    assert "alert_am_end" not in bad

    # Exactly 64 characters clears the length bound but must still fail the
    # HH:MM type check rather than slipping through on length alone.
    exactly_64 = "0" * 64
    updates, bad = main._apply_setting_updates(
        {"alert_am_start": exactly_64}, allowed)
    assert "alert_am_start" in bad
