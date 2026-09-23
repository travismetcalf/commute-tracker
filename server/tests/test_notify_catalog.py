from app import notify


def test_every_channel_has_a_default():
    for n in notify.CATALOG:
        assert set(n.defaults) == set(n.channels), n.key


def test_trip_completed_is_push_only():
    """It fires from a web request, and .dockerignore keeps scripts/ out of
    the image, so the container cannot send email. Declared, not worked around."""
    assert notify.BY_KEY["trip_completed"].channels == (notify.PUSH,)


def test_system_is_email_only():
    assert notify.BY_KEY["system"].channels == (notify.EMAIL,)


def test_setting_key_shape():
    assert notify.setting_key("brief", notify.EMAIL) == "notify_brief_email"


def test_shipping_defaults():
    d = notify.defaults()
    assert d["notify_brief_email"] == "0"
    assert d["notify_brief_push"] == "0"
    assert d["notify_weekly_summary_email"] == "1"
    assert d["notify_trip_completed_push"] == "1"
    assert d["notify_anomaly_push"] == "1"
    assert d["notify_anomaly_email"] == "0"
    assert d["notify_system_email"] == "1"


def test_no_default_for_an_unsupported_channel():
    assert "notify_trip_completed_email" not in notify.defaults()


def test_every_derived_key_has_a_validator():
    assert set(notify.validators()) == set(notify.defaults())


def test_validator_is_a_type_not_a_whitelist():
    v = notify.validators()["notify_brief_email"]
    assert v("0") and v("1")
    assert not v("2")
    assert not v("")
    assert not v("true")
    assert not v("1; rm -rf /")


def test_payload_carries_the_settings_key_and_default(conn):
    from app import notify
    rows = {r["key"]: r for r in notify.payload(conn)}

    brief = rows["brief"]
    by_channel = {s["channel"]: s for s in brief["switches"]}
    assert by_channel["email"]["setting_key"] == "notify_brief_email"
    assert by_channel["push"]["setting_key"] == "notify_brief_push"
    assert by_channel["email"]["default"] == "0"
    assert by_channel["email"]["enabled"] is False


def test_payload_switches_match_the_declared_channels(conn):
    from app import notify
    for row in notify.payload(conn):
        assert [s["channel"] for s in row["switches"]] == row["channels"]


def test_push_only_type_has_exactly_one_switch(conn):
    from app import notify
    rows = {r["key"]: r for r in notify.payload(conn)}
    assert [s["channel"] for s in rows["trip_completed"]["switches"]] == ["push"]
    assert [s["channel"] for s in rows["system"]["switches"]] == ["email"]


def test_every_setting_key_in_the_payload_is_settable(conn):
    """A key the UI renders a control for must be one the save route accepts,
    or the control 400s on first use."""
    from app import main, notify
    for row in notify.payload(conn):
        for s in row["switches"]:
            assert s["setting_key"] in main._SETTING_VALIDATORS, s["setting_key"]


def test_help_text_does_not_point_users_at_source_they_cannot_see():
    """Every `help` string in the catalog is rendered verbatim on the web
    Settings page and served to iOS through /api/v1/config — end-user text,
    not a developer aid. It must never tell the reader to go look at "the
    comment above this entry" or similar: a public-snapshot reader (and any
    app user, full stop) has no way to see a source-code comment. The
    reasoning belongs in an actual Python comment near the entry, not folded
    into the string that ships to users."""
    banned_phrases = ("comment above", "see the comment", "source comment")
    for n in notify.CATALOG:
        lowered = n.help.lower()
        for phrase in banned_phrases:
            assert phrase not in lowered, (
                f"{n.key}'s help text references source the user can't see: "
                f"{n.help!r}")
