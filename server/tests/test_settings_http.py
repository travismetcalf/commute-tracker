"""HTTP-level coverage for the bearer-gated /api/v1/settings and
/api/v1/config surface.

Every other test calls the route functions directly (api_config(None),
_apply_setting_updates(...), etc.) — nothing before this issued a real
request through FastAPI's routing and require_bearer's dependency, so the
401 gate, the bad_json path, and the exact response shapes were unexercised.
This matters because a live iOS app on a real phone posts a fixed five-key
payload to this endpoint and cannot be updated in lockstep with the server.
"""

import pytest
from fastapi.testclient import TestClient

from app import main

TOKEN = "test-bearer-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# The exact payload shipped iOS AlertSettingsView.swift posts to
# /api/v1/settings (see its `[String: String]` body literal).
IOS_ALERT_PAYLOAD = {
    "alert_days": "0,1,2,3,4",
    "alert_am_start": "06:30",
    "alert_am_end": "10:00",
    "alert_pm_start": "15:30",
    "alert_pm_end": "19:00",
}


@pytest.fixture
def client(conn, monkeypatch):
    # API_BEARER_TOKEN is read from os.environ into a module-level constant
    # at import time, so patching the env var now would have no effect —
    # patch the constant require_bearer() actually reads.
    monkeypatch.setattr(main, "API_BEARER_TOKEN", TOKEN)
    return TestClient(main.app)


def test_settings_401_without_bearer(client):
    r = client.post("/api/v1/settings", json={})
    assert r.status_code == 401


def test_config_401_without_bearer(client):
    r = client.get("/api/v1/config")
    assert r.status_code == 401


def test_settings_accepts_the_shipped_ios_payload(client):
    r = client.post("/api/v1/settings", json=IOS_ALERT_PAYLOAD, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert set(body["updated"]) == set(IOS_ALERT_PAYLOAD)


def test_settings_bad_value_shape(client):
    r = client.post("/api/v1/settings",
                     json={"alert_am_start": "25:00"}, headers=AUTH)
    assert r.status_code == 400
    body = r.json()
    assert body["status"] == "bad_value"
    assert "alert_am_start" in body["keys"]


def test_settings_bad_json_shape(client):
    r = client.post("/api/v1/settings", content=b"{not valid json",
                     headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json() == {"status": "bad_json"}


def test_config_includes_alerts_and_notifications(client):
    r = client.get("/api/v1/config", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "alerts" in body
    assert "notifications" in body
    assert set(body["alerts"]) == set(IOS_ALERT_PAYLOAD)


def test_settings_value_returns_a_fragment_for_htmx(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    client = TestClient(main.app)
    r = client.post("/settings/value",
                    data={"key": "notify_brief_email", "value": "1"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text.lower(), "returned a whole page, not a fragment"
    assert "aria-pressed=\"true\"" in r.text


def test_settings_value_pins_the_off_direction_too(conn):
    """F2: the assertion above only ever posts value=1 and checks for
    aria-pressed="true" — a rendering that hardcoded every switch to ON
    (ignoring the stored/posted value entirely) would still pass it. Post a
    switch OFF and require the fragment actually says so, with no stray
    aria-pressed="true" anywhere in it — trip_completed_push is a single
    push-only row, so there is exactly one switch in this fragment and
    nothing else to get confused with."""
    from fastapi.testclient import TestClient
    from app import main

    client = TestClient(main.app)
    r = client.post("/settings/value",
                    data={"key": "notify_trip_completed_push", "value": "0"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert 'aria-pressed="false"' in r.text
    assert 'aria-pressed="true"' not in r.text


def test_settings_value_still_redirects_without_htmx(conn):
    from fastapi.testclient import TestClient
    from app import main

    client = TestClient(main.app, follow_redirects=False)
    r = client.post("/settings/value",
                    data={"key": "notify_brief_email", "value": "1"})
    assert r.status_code == 303
    assert r.headers["location"] == "/settings"


def test_a_bad_value_is_rejected_without_being_swallowed_over_htmx(conn):
    """F1: htmx never swaps a 4xx response, and this branch adds no htmx
    error handler — so a bare HTTPException(400) here used to leave the bad
    text sitting in the field with no signal at all. The user believed it
    saved. The fix is 200 (so htmx swaps), the row read back from STORED
    truth (proving the bad value was never written), and the rejection
    visible via an `error` span beside `saved`."""
    from fastapi.testclient import TestClient
    from app import main
    from app.db import get_setting

    client = TestClient(main.app)
    r = client.post("/settings/value",
                    data={"key": "notify_brief_email", "value": "yes"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text.lower(), "returned a whole page, not a fragment"
    # notify_brief_email defaults to OFF and nothing valid was ever posted —
    # the rejected value must not have been written.
    assert get_setting(conn, "notify_brief_email", "0") == "0"
    assert 'aria-pressed="false"' in r.text, "must read back stored truth, not the rejected value"
    assert 'class="saved"' not in r.text, "a rejected save is not a save"
    assert 'class="error"' in r.text, "the rejection must be visible, not silent"


def test_a_bad_value_redirects_with_the_reason_when_not_htmx(conn):
    """Same rejection, non-htmx path: the redirect-with-reason treatment the
    Places form already uses (main.py's `_SETTINGS_ERRORS`/`?err=`), not a
    bare 4xx that dead-ends on a JSON page."""
    from fastapi.testclient import TestClient
    from app import main
    from app.db import get_setting

    client = TestClient(main.app, follow_redirects=False)
    r = client.post("/settings/value",
                    data={"key": "notify_brief_email", "value": "yes"})
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?err=bad_value"
    assert get_setting(conn, "notify_brief_email", "0") == "0"

    # And the banner the redirect lands on actually shows a reason.
    r2 = client.get(r.headers["location"])
    assert r2.status_code == 200
    assert "didn" in r2.text.lower() or "look right" in r2.text.lower()
    # Regression guard: `partials/setting_row.html` is `{% include %}`d by
    # settings.html for every row, and Jinja includes inherit the CURRENT
    # CONTEXT by default — a row partial that keyed its inline rejection
    # span off the SAME name as the page-level banner (`error`) would leak
    # this unrelated bad_value message onto every single row on the page,
    # not just show it in the banner. It must not.
    assert 'class="error"' not in r2.text


def test_every_settable_key_has_a_row(conn):
    """A key the save route accepts but no row can render would 500 on
    save. _SETTING_VALIDATORS and the catalog derive from the same source
    today; this asserts they stay in step."""
    from app import main

    for key in main._SETTING_VALIDATORS:
        main._row_context(conn, key)   # raises KeyError if it has no home


def test_a_channel_gapped_row_shows_the_placeholder_not_a_second_button(conn):
    """trip_completed is push-only and system is email-only. The missing
    channel must render as a matrix__none placeholder — that's what keeps
    the matrix columns aligned and marks the gap as deliberate — not as a
    second live button, and not as nothing at all."""
    from fastapi.testclient import TestClient
    from app import main

    client = TestClient(main.app)

    r = client.post("/settings/value",
                    data={"key": "notify_system_email", "value": "1"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "matrix__none" in r.text
    assert r.text.count("hx-post=") == 1

    r = client.post("/settings/value",
                    data={"key": "notify_trip_completed_push", "value": "1"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "matrix__none" in r.text
    assert r.text.count("hx-post=") == 1


def test_gapped_row_places_the_placeholder_in_the_correct_column(conn):
    """F4: the test above proves ONE button and ONE placeholder render for a
    channel-gapped row, but never checks which column each lands in —
    _switch_columns() building (push, email) instead of (email, push) would
    still pass it, and the email-only `system` row would render its switch
    under Push with an em dash under Email. The aria-label spells out its own
    channel ("<label> by email"/"by push"), so the ORDER those substrings
    appear in the fragment is exactly the column order rendered."""
    import re
    from fastapi.testclient import TestClient
    from app import main

    client = TestClient(main.app)

    r = client.post("/settings/value",
                    data={"key": "notify_system_email", "value": "1"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    tokens = re.findall(r"matrix__none|by email|by push", r.text)
    assert tokens == ["by email", "matrix__none"], (
        f"system (email-only) must render its switch under Email and a "
        f"placeholder under Push, got {tokens}")

    r = client.post("/settings/value",
                    data={"key": "notify_trip_completed_push", "value": "1"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    tokens = re.findall(r"matrix__none|by email|by push", r.text)
    assert tokens == ["matrix__none", "by push"], (
        f"trip_completed (push-only) must render a placeholder under Email "
        f"and its switch under Push, got {tokens}")


def test_static_assets_are_cache_busted(client):
    """A bare /static/css/app.css is a stale-cache trap: StaticFiles sends no
    cache-control, so a browser applies heuristic freshness and can serve a
    pre-deploy stylesheet for weeks against post-deploy markup — which is
    exactly how the settings redesign first rendered as unstyled text.

    Mutation that makes this RED: drop `?v={{ asset_v }}` from either tag in
    base.html, or stop setting templates.env.globals["asset_v"].
    """
    import re
    r = client.get("/settings")
    assert r.status_code == 200
    for asset in ("/static/css/app.css", "/static/js/htmx.min.js"):
        m = re.search(re.escape(asset) + r"\?v=([0-9a-f]+)", r.text)
        assert m, f"{asset} is not cache-busted"
        assert m.group(1), f"{asset} has an empty version stamp"
