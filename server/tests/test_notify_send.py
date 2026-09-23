import sys

import pytest

import app as app_package
from app import notify
from app.db import set_setting


@pytest.fixture
def spy(monkeypatch):
    """Records what each channel was asked to do, so the tests assert on
    dispatch rather than on real email or real APNs."""
    calls = {"email": [], "push": []}

    def fake_email(subject, text, html=None, from_addr=None, to_addr=None):
        calls["email"].append(
            {"subject": subject, "text": text, "html": html,
             "from": from_addr, "to": to_addr})
        return "sent"

    class FakePush:
        ok = True
        configured_flag = True

        @staticmethod
        def configured():
            return FakePush.configured_flag

        @staticmethod
        def send_alert(title, body, extra=None):
            calls["push"].append({"title": title, "body": body, "extra": extra})
            return 1 if FakePush.ok else 0

    monkeypatch.setattr(notify, "_send_email", fake_email)
    # Two patches, not one. notify.send() does `from app import push` at call
    # time, which Python resolves as an ATTRIBUTE lookup on the already-loaded
    # `app` package first, falling back to sys.modules only when the package
    # has no such attribute. Today nothing binds `push` onto `app` before this
    # runs, so patching sys.modules["app.push"] alone is enough. But the
    # moment anything else does a module-level `from app import push` (e.g.
    # main.py firing a push from /api/v1/ingest/events), pytest collection
    # will already have bound the REAL push module onto `app.push` as a side
    # effect, and the attribute lookup wins over sys.modules — silently
    # defeating this fixture (a dev box with no APNs configured would make
    # test_unconfigured_push_is_not_a_failure keep passing, but for the wrong
    # reason). Patching both keeps this fixture correct either way.
    monkeypatch.setitem(sys.modules, "app.push", FakePush)
    monkeypatch.setattr(app_package, "push", FakePush, raising=False)
    calls["push_cls"] = FakePush
    return calls


def test_disabled_channels_are_not_called(conn, spy):
    r = notify.send(conn, "brief", "S", "B")
    assert r == {"email": "disabled", "push": "disabled"}
    assert spy["email"] == [] and spy["push"] == []


def test_enabled_channel_is_called(conn, spy):
    r = notify.send(conn, "weekly_summary", "Commute week", "body text")
    assert r["email"] == "sent"
    assert r["push"] == "disabled"
    assert spy["email"][0]["subject"] == "Commute week"


def test_sender_overrides_are_passed_through(conn, spy):
    """brief.sh's sender discipline depends on --from reaching the notifier."""
    set_setting(conn, "notify_brief_email", "1")
    notify.send(conn, "brief", "S", "B",
                from_addr="a@example.com", to_addr="a@example.com")
    assert spy["email"][0]["from"] == "a@example.com"
    assert spy["email"][0]["to"] == "a@example.com"


def test_push_uses_subject_as_title_and_text_as_body(conn, spy):
    r = notify.send(conn, "trip_completed", "Trip home · 58 min", "6 min longer")
    assert r == {"push": "sent"}
    assert spy["push"][0] == {"title": "Trip home · 58 min", "body": "6 min longer",
                              "extra": None}


def test_unconfigured_push_is_not_a_failure(conn, spy):
    """APNs not being set up is a deployment state the job cannot fix."""
    spy["push_cls"].configured_flag = False
    r = notify.send(conn, "trip_completed", "S", "B")
    assert r == {"push": "unconfigured"}
    assert notify.failed(r) is False


def test_a_push_that_reaches_no_device_is_a_failure(conn, spy):
    spy["push_cls"].ok = False
    r = notify.send(conn, "trip_completed", "S", "B")
    assert r == {"push": "failed"}
    assert notify.failed(r) is True


def test_disabled_everything_is_not_a_failure(conn, spy):
    assert notify.failed(notify.send(conn, "brief", "S", "B")) is False


def test_unknown_type_raises(conn, spy):
    with pytest.raises(KeyError):
        notify.send(conn, "no_such_type", "S", "B")


def test_push_delivery_exception_degrades_to_failed(conn, spy, monkeypatch):
    """push.send_alert can raise for real — a truncated .p8 out of
    push._bearer(), a lock timeout out of push._send()'s own get_conn().
    That is a delivery fault, not a programming error, so send() must not
    let it propagate (see notify.py's PUSH branch and anomaly-check.py's
    handler, which relies on this to avoid a duplicate-alert storm)."""
    def raising_send_alert(title, body, extra=None):
        raise RuntimeError("boom: corrupt .p8")

    monkeypatch.setattr(spy["push_cls"], "send_alert",
                        staticmethod(raising_send_alert))
    r = notify.send(conn, "trip_completed", "S", "B")
    assert r == {"push": "failed"}
    assert notify.failed(r) is True


def test_push_delivery_exception_does_not_affect_email_verdict(conn, spy, monkeypatch):
    """anomaly's channels are (EMAIL, PUSH) — email dispatches first. A push
    exception in the same send() call must not touch the email verdict
    already recorded."""
    def raising_send_alert(title, body, extra=None):
        raise RuntimeError("boom: corrupt .p8")

    monkeypatch.setattr(spy["push_cls"], "send_alert",
                        staticmethod(raising_send_alert))
    set_setting(conn, "notify_anomaly_email", "1")
    r = notify.send(conn, "anomaly", "S", "B")
    assert r["email"] == "sent"
    assert r["push"] == "failed"
    assert spy["email"][0]["subject"] == "S"


def test_push_delivery_exception_detail_reaches_stderr_not_stdout(conn, spy, monkeypatch, capsys):
    def raising_send_alert(title, body, extra=None):
        raise RuntimeError("boom: corrupt .p8")

    monkeypatch.setattr(spy["push_cls"], "send_alert",
                        staticmethod(raising_send_alert))
    r = notify.send(conn, "trip_completed", "S", "B")
    assert r == {"push": "failed"}

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "RuntimeError" in captured.err
    assert "boom: corrupt .p8" in captured.err


def test_extra_reaches_push(conn, spy):
    """notify.send forwards extra as far as push.send_alert's call boundary.
    This is the shallow half of the guarantee — it uses the `spy` fixture's
    FakePush stand-in, so it says nothing about what the real push.send_alert
    does with extra once it has it. See test_send_alert_merges_extra_* below
    for the real merge, and test_extra_does_not_reach_email for the email
    side, which trip_completed's push-only channel set can't exercise."""
    notify.send(conn, "trip_completed", "T", "B", extra={"trip_key": "k1"})
    assert spy["push"][0].get("extra") == {"trip_key": "k1"}


def test_extra_does_not_reach_email(conn, spy):
    """trip_completed's channels are (PUSH,) only, so a test built on it can
    never actually exercise the email arm — it would pass whether or not
    extra leaked into email, because email is never called at all. anomaly is
    (EMAIL, PUSH), so enabling both channels here gives this something to
    fail if _send_email ever grows an extra/trip_key parameter and notify.py
    starts passing one."""
    set_setting(conn, "notify_anomaly_email", "1")
    r = notify.send(conn, "anomaly", "S", "B", extra={"trip_key": "x"})
    assert r["email"] == "sent"
    assert r["push"] == "sent"
    assert spy["push"][0]["extra"] == {"trip_key": "x"}
    assert "extra" not in spy["email"][0]
    assert "trip_key" not in spy["email"][0]


def test_send_alert_merges_extra_as_top_level_payload_key(monkeypatch):
    """Exercises the REAL push.send_alert, not the `spy` fixture's FakePush
    stand-in — the `spy`-based tests above prove only that notify.send passes
    extra as an argument; they never run push.py's own payload.update(extra),
    so a broken merge there would slip past every test above. Monkeypatches
    push._send (the seam right after the payload dict is built) to capture
    what send_alert actually constructs, and push.configured() so send_alert
    does not bail out before reaching it."""
    from app import push

    captured = {}

    def fake_send(payload, push_type, priority):
        captured["payload"] = payload
        return 1

    monkeypatch.setattr(push, "configured", lambda: True)
    monkeypatch.setattr(push, "_send", fake_send)

    push.send_alert("T", "B", extra={"trip_key": "abc"})

    payload = captured["payload"]
    # A top-level sibling of "aps", not nested inside it — APNs only reads
    # custom keys at the payload's top level; a key buried inside aps is
    # invisible to didReceive's userInfo on the phone.
    assert payload["trip_key"] == "abc"
    assert "trip_key" not in payload["aps"]
    assert payload["aps"]["alert"]["title"] == "T"
    assert payload["aps"]["alert"]["body"] == "B"


def test_send_alert_guards_against_an_extra_named_aps(monkeypatch):
    """An `extra` entry named "aps" would silently clobber the alert dict
    built above via plain payload.update(extra) — APNs would then deliver a
    payload with no visible alert at all. Not reachable through any caller
    today, but guarded so it stays that way rather than depending on future
    callers never choosing that key."""
    from app import push

    captured = {}

    def fake_send(payload, push_type, priority):
        captured["payload"] = payload
        return 1

    monkeypatch.setattr(push, "configured", lambda: True)
    monkeypatch.setattr(push, "_send", fake_send)

    push.send_alert("T", "B", extra={"aps": {"content-available": 1},
                                     "trip_key": "abc"})

    payload = captured["payload"]
    assert payload["aps"]["alert"]["title"] == "T"
    assert payload["aps"]["alert"]["body"] == "B"
    assert payload["aps"]["sound"] == "default"
    assert payload["trip_key"] == "abc"


def test_send_alert_without_extra_omits_trip_key(monkeypatch):
    """The no-extra path (every caller but trip_notify today) must build the
    exact same payload as before extra existed — no stray key, no changed
    shape."""
    from app import push

    captured = {}

    def fake_send(payload, push_type, priority):
        captured["payload"] = payload
        return 1

    monkeypatch.setattr(push, "configured", lambda: True)
    monkeypatch.setattr(push, "_send", fake_send)

    push.send_alert("T", "B")

    payload = captured["payload"]
    assert "trip_key" not in payload
    assert payload == {"aps": {"alert": {"title": "T", "body": "B"},
                               "sound": "default"}}


def test_missing_email_shim_reports_unavailable(conn, monkeypatch):
    """The container has no scripts/ — an honest verdict, not a silent success."""
    monkeypatch.setattr(notify, "_SHIM", "/nonexistent/notify-email.sh")
    set_setting(conn, "notify_brief_email", "1")
    r = notify.send(conn, "brief", "S", "B")
    assert r["email"] == "unavailable"
    assert notify.failed(r) is True
