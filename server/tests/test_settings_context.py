from app import main
from app.brief import ALERT_DEFAULTS
from app.db import set_setting


def _ctx(conn):
    """The settings dict the template would receive."""
    return main._settings_for_template(conn)


def test_alert_keys_resolve_even_when_no_row_exists(conn):
    """They live in ALERT_DEFAULTS, not DEFAULT_SETTINGS, so a fresh database
    has no row — and Jinja renders a missing key as the empty string."""
    for key, default in ALERT_DEFAULTS.items():
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM settings WHERE key=?", (key,)
        ).fetchone()["n"] == 0, f"{key} unexpectedly seeded"

    ctx = _ctx(conn)
    for key, default in ALERT_DEFAULTS.items():
        assert ctx[key] == default, f"{key} rendered as {ctx[key]!r}"


def test_a_stored_value_wins_over_the_default(conn):
    set_setting(conn, "alert_am_start", "05:45")
    conn.commit()
    assert _ctx(conn)["alert_am_start"] == "05:45"


def test_notification_keys_resolve_too(conn):
    from app import notify
    ctx = _ctx(conn)
    for key, default in notify.defaults().items():
        assert ctx[key] == default, f"{key} rendered as {ctx[key]!r}"


def test_seeded_keys_still_come_from_the_table(conn):
    assert _ctx(conn)["monthly_call_cap"] == "18000"


def test_groups_cover_every_tunable_exactly_once(conn):
    """A key in no group is invisible; a key in two renders twice and the two
    controls fight over the same value."""
    seen = [k for g in main.SETTING_GROUPS for (k, _label, _help) in g["rows"]]
    assert len(seen) == len(set(seen)), "a key appears in more than one group"

    tunables = {
        "monthly_call_cap", "mpg", "fuel_price_per_gal", "office_days_per_week",
        "office_hours_per_day", "earliest_leave_home", "latest_leave_office",
        "anomaly_min_delta_min", "anomaly_min_live_days", *ALERT_DEFAULTS,
    }
    assert tunables - set(seen) == set(), "these keys lost their home"


def test_every_grouped_key_is_settable(conn):
    for g in main.SETTING_GROUPS:
        for (key, _l, _h) in g["rows"]:
            assert key in main._SETTING_VALIDATORS, key


def test_settings_page_renders_the_groups_and_the_matrix(conn):
    """The page is the deliverable: five titled groups and a switch matrix,
    not the fourteen-row Tunables card.

    Built inline from `conn` rather than a shared `client` fixture: `client`
    is defined in test_settings_http.py, and pytest does not share fixtures
    across test modules without promoting it to conftest.py. Every other test
    in this file already builds its subject directly from `conn`, so this
    stays consistent with that rather than introducing a cross-file fixture
    for one test.
    """
    from fastapi.testclient import TestClient
    r = TestClient(main.app).get("/settings")
    assert r.status_code == 200
    for title in ("Notifications", "Your commute", "Planning",
                  "Commute alerts", "System", "Places"):
        assert title in r.text, title
    assert "Tunables" not in r.text
    assert "matrix__head" in r.text
    # Every notification type reaches the page as a row of the shared partial.
    from app import notify as notify_mod
    for n in notify_mod.CATALOG:
        assert f'id="set-{n.key}"' in r.text, n.key


def test_settings_page_matrix_renders_actual_switches_not_just_row_ids(conn):
    """F3: the test above proves `matrix__head` exists and each notification
    type has a row `id="set-<key>"` — but `matrix__head` is a static table
    header unconditionally present in the template, and a row's id renders
    from its label/help regardless of whether _switch_columns() returned
    anything. `"channels": _switch_columns(n)` collapsing to `"channels": []`
    for every type would still pass that test while the matrix shows zero
    controls. Every notification type contributes exactly two channel slots
    (email + push) — a live switch or a matrix__none placeholder, never
    neither — so the slot count is pinned exactly, not just asserted
    non-zero.
    """
    from fastapi.testclient import TestClient
    from app import notify as notify_mod

    r = TestClient(main.app).get("/settings")
    assert r.status_code == 200
    n_types = len(notify_mod.CATALOG)
    switches = r.text.count('class="switch"')
    placeholders = r.text.count("matrix__none")
    assert switches + placeholders == n_types * 2, (
        f"expected {n_types * 2} channel slots (2 per notification type), "
        f"got {switches} switches + {placeholders} placeholders")
    assert switches > 0, "the matrix rendered zero live switches"


def test_settings_page_pins_switch_state_off_when_everything_is_stored_off(conn):
    """F2: nothing previously pinned rendered switch state in the OFF
    direction — the page could render all 8 notification switches ON while
    every stored value was OFF, and the suite stayed green (the only
    coverage posted value=1 and checked for aria-pressed="true"). Force
    every notify_* key to '0' and require the page to read it back, not
    just to have switches at all (that half is F3, above)."""
    from fastapi.testclient import TestClient
    from app import notify as notify_mod

    for key in notify_mod.defaults():
        set_setting(conn, key, "0")
    conn.commit()

    r = TestClient(main.app).get("/settings")
    assert r.status_code == 200
    assert 'aria-pressed="true"' not in r.text, (
        "every notification switch is stored OFF but the page rendered at "
        "least one as ON")
    assert r.text.count('aria-pressed="false"') == len(notify_mod.defaults())


def test_settings_page_orders_sections_as_declared(conn):
    """Membership is not enough — the test above passed with Places rendered
    BEFORE Notifications, which is the exact ordering this task exists to fix
    (spec line 212: Notifications, Places, then the four SETTING_GROUPS in
    their declared order). Assert the sequence, not just that each title is
    somewhere in the document — equality also catches a title being dropped
    or duplicated, which "each appears after the previous one" would not.
    """
    import re
    from fastapi.testclient import TestClient
    r = TestClient(main.app).get("/settings")
    titles = re.findall(r'<h2 class="section__title">([^<]+)</h2>', r.text)
    assert titles == ["Notifications", "Places", "Your commute", "Planning",
                       "Commute alerts", "System"]


def test_settings_page_grouped_rows_are_never_blank(conn):
    """The Task 2 bug — alert_* rows rendering with value="" because they
    live in ALERT_DEFAULTS/notify.defaults() rather than db.DEFAULT_SETTINGS
    — must not be able to come back invisibly through _row_context. On a
    fresh throwaway database every grouped key resolves non-empty (measured:
    office_days_per_week='3', office_hours_per_day='8.0', mpg='28',
    fuel_price_per_gal='3.60', earliest_leave_home='07:00',
    latest_leave_office='18:00', alert_days='0,1,2,3,4',
    alert_am_start='06:30', alert_am_end='10:00', alert_pm_start='15:30',
    alert_pm_end='19:00', anomaly_min_delta_min='10',
    anomaly_min_live_days='21', monthly_call_cap='18000'), so there is no
    legitimately-blank row to special-case here.

    The extraction matches the specific `<input>` for this key via its
    `hx-vals='{"key": "<key>"}'` attribute (unique per row) and captures its
    `value="..."` — NOT a `id="set-<key>".*?</div>\\s*</div>` scan. That
    pattern (the plan's own Step 5 verification script uses it too, and the
    prior Task 5 report trusted it) stops at the FIRST nested
    `</div>\\s*</div>`, which closes `.setting__text` two levels before the
    `<input>` in `.setting__ctl` — the value it is supposedly scanning for is
    outside the substring it captures, so it can never fire regardless of
    what the input actually holds. Verified by hand: with `_row_context`
    patched to always return `value=""`, the old pattern's captured group for
    the 'mpg' row was `'id="set-mpg">\\n  <div class="setting__text">\\n
    <div class="setting__label">Vehicle MPG</div>\\n    <div
    class="setting__help">For the fuel cost line</div>\\n  </div>'` — no
    `<input>` in it at all.
    """
    import re
    from fastapi.testclient import TestClient
    r = TestClient(main.app).get("/settings")
    blank = []
    for g in main.SETTING_GROUPS:
        for (key, _l, _h) in g["rows"]:
            pattern = (r'<input\s+name="value"\s+value="([^"]*)"[^>]*'
                       r'hx-vals=\'\{"key":\s*"' + re.escape(key) + r'"\}\'[^>]*>')
            m = re.search(pattern, r.text)
            assert m, f"{key} did not render as a field row at all"
            if m.group(1) == "":
                blank.append(key)
    assert not blank, f"these settings rendered blank: {blank}"


def test_settings_page_shows_the_api_budget_under_system(conn):
    """The System group's context is the live API-budget readout, wired in
    settings_page() by matching g["title"] == "System" rather than indexing
    SETTING_GROUPS positionally. Title-matching fails closed instead of
    silently retargeting another group when the list is reordered — but
    "fails closed" means the budget line just vanishes with no signal, not
    that it can't happen (e.g. a rename of the System group's title). This
    is the signal.
    """
    from fastapi.testclient import TestClient
    r = TestClient(main.app).get("/settings")
    assert "API budget used this month:" in r.text
