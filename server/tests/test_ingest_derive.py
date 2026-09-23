import time

from fastapi.testclient import TestClient

from app import main
from app.db import get_conn


def _ev(key, kind, ts, place=None):
    e = {"event_key": key, "kind": kind, "source": "test", "device_ts": ts}
    if place:
        e["place_key"] = place
    return e


def test_a_new_end_event_requests_a_derive():
    assert main._should_derive([{"event_key": "a", "status": "ok"}],
                               [_ev("a", "geofence_enter", 1, "home")]) is True


def test_a_duplicate_end_event_does_not():
    """An outbox replay of already-delivered events must not re-derive."""
    assert main._should_derive([{"event_key": "a", "status": "duplicate"}],
                               [_ev("a", "geofence_enter", 1, "home")]) is False


def test_a_start_event_alone_does_not():
    assert main._should_derive([{"event_key": "a", "status": "ok"}],
                               [_ev("a", "geofence_leave", 1, "home")]) is False


def test_a_rejected_event_does_not():
    assert main._should_derive([{"event_key": "a", "status": "rejected"}],
                               [_ev("a", "geofence_enter", 1, "home")]) is False


def test_a_mixed_batch_derives_once():
    """An outbox replay of mixed events triggers one derive, not one per
    event, no matter how many non-ending events ride along."""
    events = [_ev(str(i), "geofence_leave", i) for i in range(10)]
    events += [_ev("end", "drive_end", 99)]
    results = [{"event_key": e["event_key"], "status": "ok"} for e in events]
    assert main._should_derive(results, events) is True


def test_notify_runs_outside_the_ingest_transaction(conn, monkeypatch):
    """A write from inside the notifier must succeed immediately. If the
    handler still held its write transaction open, this would block for the
    full busy_timeout and then raise 'database is locked' — measured at 10.47s.

    So the assertion is on ELAPSED TIME as well as success: a passing-but-slow
    run means the write went through only because the transaction happened to
    close first, which is exactly the fragility this placement removes.
    """
    monkeypatch.setattr(main, "API_BEARER_TOKEN", "t" * 32)
    monkeypatch.setattr("app.derive.derive", lambda *a, **k: {"complete": 0})

    seen = {}

    def fake_notify():
        started = time.monotonic()
        with get_conn() as c:
            c.execute("UPDATE devices SET enabled=0 WHERE id=-1")
        seen["elapsed"] = time.monotonic() - started
        return []

    monkeypatch.setattr("app.trip_notify.notify_completed_trips", fake_notify)

    client = TestClient(main.app)
    r = client.post(
        "/api/v1/ingest/events",
        headers={"Authorization": "Bearer " + "t" * 32},
        json={"events": [{"event_key": "e1" * 8, "kind": "geofence_enter",
                          "source": "ios", "device_ts": 1789000000,
                          "place_key": "home"}]},
    )

    assert r.status_code == 200
    assert seen.get("elapsed") is not None, "the notifier was never reached"
    assert seen["elapsed"] < 1.0, (
        f"notifier write took {seen['elapsed']:.2f}s — it is running inside "
        "the ingest transaction")


def test_a_notify_failure_does_not_fail_the_ingest(conn, monkeypatch):
    """The events are durably stored by then. A non-2xx would make the phone
    retry a batch it already delivered."""
    monkeypatch.setattr(main, "API_BEARER_TOKEN", "t" * 32)
    monkeypatch.setattr("app.derive.derive", lambda *a, **k: {"complete": 0})
    monkeypatch.setattr("app.trip_notify.notify_completed_trips",
                        lambda: (_ for _ in ()).throw(RuntimeError("apns down")))

    client = TestClient(main.app)
    r = client.post(
        "/api/v1/ingest/events",
        headers={"Authorization": "Bearer " + "t" * 32},
        json={"events": [{"event_key": "e2" * 8, "kind": "geofence_enter",
                          "source": "ios", "device_ts": 1789000000,
                          "place_key": "home"}]},
    )
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == "ok"
