from datetime import datetime, timedelta

import pytest

from app import notify as notify_mod
from app import trip_notify
from app.db import LOCAL_TZ, set_setting
from app.derive import derive
from app.stats import trip_bucket

from tests.trip_fixtures import add_trip, seed_topology


def _now_after(day, depart_mod, duration_sec, extra_min=1):
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    return (d + timedelta(minutes=depart_mod, seconds=duration_sec)
              + timedelta(minutes=extra_min))


def _one_trip(conn, needs_review=False, day="2026-09-14",
              depart_mod=8 * 60, duration_sec=2400, heading="outbound"):
    """Build one derived trip and PROVE it exists before returning.

    Six tests below assert `candidates(...) == []`. An empty list is also
    exactly what you get from a fixture that silently built nothing — so
    without this guard those tests would pass for the wrong reason and the
    dedupe logic they are meant to protect would be unverified. That failure
    mode has already occurred twice in this project; the guard lives here so
    every caller inherits it rather than each remembering.
    """
    seed_topology(conn)
    key = add_trip(conn, day=day, depart_mod=depart_mod,
                   duration_sec=duration_sec, heading=heading,
                   needs_review=needs_review)
    conn.commit()
    derive()
    row = conn.execute(
        "SELECT status, duration_sec, needs_review FROM trips "
        "WHERE start_event_key=?", (key,)).fetchone()
    assert row is not None, f"fixture built no trip for {key}"
    assert row["status"] == "complete", f"fixture trip is {row['status']}"
    assert row["duration_sec"] == duration_sec
    assert row["needs_review"] == (1 if needs_review else 0)
    return key, _now_after(day, depart_mod, duration_sec)


def test_a_recent_complete_trip_is_a_candidate(conn):
    key, now = _one_trip(conn)
    cands = trip_notify.candidates(conn, now=now)
    assert [c["start_event_key"] for c in cands] == [key]


def test_an_old_trip_is_not(conn):
    """The recency gate is what stops an outbox replay or a hand-entered
    backfill from firing a burst of stale notifications.

    The 45-minute offset below is FIXED, not derived from RECENT_WINDOW_MIN —
    a probe time computed as `now + RECENT_WINDOW_MIN + 1` moves in lockstep
    with the constant it's supposed to be testing, so inflating the constant
    would move the cutoff by exactly the same amount and this test would stay
    green no matter how wide the window got. The separate constant assertion
    below exists so a deliberate change to RECENT_WINDOW_MIN fails loudly with
    an explanation, rather than this test silently testing nothing.
    """
    key, now = _one_trip(conn)
    assert trip_notify.RECENT_WINDOW_MIN == 30, (
        "this test's fixed 45-minute offset assumes a 30-minute window; "
        "if the window changed deliberately, change the offset too")
    late = now + timedelta(minutes=45)
    assert trip_notify.candidates(conn, now=late) == []


def test_a_flagged_trip_is_not(conn):
    """derive.py:39 — a confident wrong label is worse than an honest gap."""
    key, now = _one_trip(conn, needs_review=True)
    assert trip_notify.candidates(conn, now=now) == []


def test_rejecting_a_flagged_trip_must_not_make_it_eligible(conn):
    """C1: both adjudication endpoints (main.py:237 web, main.py:1134 iOS) clear
    needs_review alongside the verdict. A hand-spelled predicate here that only
    checked needs_review=0 (and never inspected verdict) flipped a flagged trip
    from ineligible to eligible the instant the user pressed Reject — announcing,
    inside RECENT_WINDOW_MIN, the exact trip the user just told the system to
    discard.

    This must use the shared COUNTS_TOWARD_AVERAGES predicate rather than a
    second hand-spelled copy of it, precisely because a hand-spelled copy is
    how this bug happened the first time.
    """
    key, now = _one_trip(conn, needs_review=True)
    assert trip_notify.candidates(conn, now=now) == [], (
        "a flagged trip must not be a candidate while awaiting review")

    # The exact UPDATE issued by set_verdict's reject path (main.py:237).
    trip_id = conn.execute(
        "SELECT id FROM trips WHERE start_event_key=?", (key,)).fetchone()[0]
    conn.execute(
        "UPDATE trips SET verdict=?, needs_review=0, verdict_by=? WHERE id=?",
        ("rejected", "test", trip_id))
    conn.commit()

    assert trip_notify.candidates(conn, now=now) == [], (
        "a trip the user REJECTED must never become a notification candidate")


def test_a_recorded_trip_is_not_a_candidate_again(conn):
    key, now = _one_trip(conn)
    assert trip_notify.record(conn, key) is True     # claim
    trip_notify.record(conn, key, verdict="sent")    # finalize
    conn.commit()
    assert trip_notify.candidates(conn, now=now) == []


def test_repeated_derives_do_not_re_offer_the_same_trip(conn):
    """derive() replays the whole log every run and upserts every trip. Without
    the side table this is the burst that would fire every 15 minutes."""
    key, now = _one_trip(conn)
    assert trip_notify.record(conn, key) is True     # claim
    trip_notify.record(conn, key, verdict="sent")    # finalize
    conn.commit()
    for _ in range(3):
        derive()
        assert trip_notify.candidates(conn, now=now) == []


def test_a_full_rebuild_does_not_re_offer(conn):
    key, now = _one_trip(conn)
    assert trip_notify.record(conn, key) is True     # claim
    trip_notify.record(conn, key, verdict="sent")    # finalize
    conn.commit()
    derive(rebuild=True)
    assert trip_notify.candidates(conn, now=now) == []


def test_recording_on_consideration_prevents_a_backlog(conn):
    """A trip considered while the switch was off must not be delivered later."""
    key, now = _one_trip(conn)
    assert trip_notify.record(conn, key) is True         # claim
    trip_notify.record(conn, key, verdict="disabled")    # finalize
    conn.commit()
    assert trip_notify.candidates(conn, now=now) == []


def test_claim_is_exclusive(conn):
    """C2: notify_completed_trips() runs from two concurrent callers in
    production (the container's ingest path and the host's 15-minute derive
    job), separated by compose() and the full APNs round trip with nothing
    claiming the trip in between. record(conn, key) with no verdict is the
    claim, and it must be exclusive: whichever caller's INSERT lands first
    against the PRIMARY KEY on start_event_key wins, and the second must be
    told it lost rather than silently proceeding to send a duplicate.

    No threads needed — the PRIMARY KEY makes this deterministic even called
    twice in sequence on one connection.
    """
    key, now = _one_trip(conn)
    assert trip_notify.record(conn, key) is True, "the first claim must win"
    conn.commit()
    assert trip_notify.record(conn, key) is False, (
        "a second claim on the same trip must report it did not win")


def test_compose_with_both_numbers(conn):
    """ORDER MATTERS: derive()'s upsert rewrites predicted_sec on every run
    (derive.py:135-142), so the manual UPDATE must come AFTER the last derive
    or it is silently wiped and the clause disappears."""
    seed_topology(conn)
    key = add_trip(conn, day="2026-09-14", depart_mod=8 * 60,
                   duration_sec=3480, heading="outbound")        # 58 min
    for d in ["2026-09-07", "2026-09-08", "2026-09-09",
              "2026-09-10", "2026-09-11"]:                       # five at 54 min
        add_trip(conn, day=d, depart_mod=8 * 60, duration_sec=3240,
                 heading="outbound")
    conn.commit()
    derive()                                                     # all derives done
    conn.execute("UPDATE trips SET predicted_sec=3120 WHERE start_event_key=?",
                 (key,))                                         # 52 min anticipated
    conn.commit()

    now = _now_after("2026-09-14", 8 * 60, 3480)
    trip = [c for c in trip_notify.candidates(conn, now=now)
            if c["start_event_key"] == key][0]
    assert trip["predicted_sec"] == 3120, "the UPDATE was wiped by a later derive"

    title, body = trip_notify.compose(conn, trip)
    assert "58 min" in title
    # F9: full word "minutes" and clauses joined with "and", matching the
    # user's own wording verbatim ("6 minutes longer than anticipated and 4
    # minutes over the average") rather than the terser "min"/comma this
    # shipped with. Exact equality (not two "in body" checks) is what pins
    # the join itself, not just the two substrings independently.
    assert body == "6 minutes longer than anticipated and 4 minutes over the average"


def test_the_trip_is_not_counted_in_its_own_average(conn):
    """Comparing a measurement against a population containing it
    understates the delta, worst at the MIN_SAMPLES floor.

    Same fixture as test_compose_with_both_numbers: one 58-min trip plus
    five 54-min trips on earlier weekdays at the same minute. Without
    exclude_key the pool is all six trips (n=6, avg 3280s) — the 58-min trip
    is a sixth of its own baseline. With exclude_key it is the five 54-min
    trips alone (n=5, avg 3240s), which is the population the message is
    actually supposed to compare against.
    """
    seed_topology(conn)
    key = add_trip(conn, day="2026-09-14", depart_mod=8 * 60,
                   duration_sec=3480, heading="outbound")        # 58 min
    for d in ["2026-09-07", "2026-09-08", "2026-09-09",
              "2026-09-10", "2026-09-11"]:                       # five at 54 min
        add_trip(conn, day=d, depart_mod=8 * 60, duration_sec=3240,
                 heading="outbound")
    conn.commit()
    derive()

    row = conn.execute(
        "SELECT route_id, local_mod FROM trips WHERE start_event_key=?",
        (key,)).fetchone()

    included = trip_bucket(conn, row["route_id"], row["local_mod"])
    assert included["n"] == 6
    assert included["avg_sec"] == 3280

    excluded = trip_bucket(conn, row["route_id"], row["local_mod"],
                           exclude_key=key)
    assert excluded["n"] == 5
    assert excluded["avg_sec"] == 3240


def test_compose_drops_the_anticipated_clause_when_null(conn):
    """predicted_sec is legitimately NULL off-hours, on baseline corridors, and
    when no live sample sits within ±10 min of departure (derive.py:99-110)."""
    key, now = _one_trip(conn, duration_sec=3480)
    conn.execute("UPDATE trips SET predicted_sec=NULL WHERE start_event_key=?",
                 (key,))
    conn.commit()
    trip = trip_notify.candidates(conn, now=now)[0]
    title, body = trip_notify.compose(conn, trip)
    assert "anticipated" not in body


def test_compose_drops_the_average_clause_below_the_floor(conn):
    key, now = _one_trip(conn, duration_sec=3480)
    conn.execute("UPDATE trips SET predicted_sec=3120 WHERE start_event_key=?",
                 (key,))
    conn.commit()
    trip = trip_notify.candidates(conn, now=now)[0]
    title, body = trip_notify.compose(conn, trip)
    assert "anticipated" in body
    assert "average" not in body
    # F9: with only one clause surviving, " and ".join must not leave a
    # stray joiner behind — pin the exact string, not just substrings.
    assert body == "6 minutes longer than anticipated"


def test_compose_with_neither_number_is_still_useful(conn):
    key, now = _one_trip(conn, duration_sec=3480)
    conn.execute("UPDATE trips SET predicted_sec=NULL WHERE start_event_key=?",
                 (key,))
    conn.commit()
    trip = trip_notify.candidates(conn, now=now)[0]
    title, body = trip_notify.compose(conn, trip)
    assert "58 min" in title
    assert body == ""


def test_inbound_says_home_outbound_names_the_place(conn):
    """places.kind is literally 'home', so 'home' is read from data, not
    hardcoded; the outbound side uses the user's own label."""
    key, now = _one_trip(conn, heading="inbound", depart_mod=17 * 60)
    title, _ = trip_notify.compose(conn, trip_notify.candidates(conn, now=now)[0])
    assert title.startswith("Trip home")

    key2, now2 = _one_trip(conn, day="2026-09-15", heading="outbound")
    out = [c for c in trip_notify.candidates(conn, now=now2)
           if c["start_event_key"] == key2][0]
    title2, _ = trip_notify.compose(conn, out)
    assert "Test Office" in title2


# ── notify_completed_trips() — the orchestrator itself (I5) ─────────────────
#
# Elsewhere in this suite notify_completed_trips() is only ever REPLACED
# (test_ingest_derive.py monkeypatches it wholesale), so its own body — the
# ordering, the connections, the try scope, claim-then-send — was executed by
# nothing. C1, C2 and I6 all lived in exactly those ~30 lines.
#
# _one_trip's synthetic dates only work when the caller passes the matching
# synthetic `now` into candidates(conn, now=...); notify_completed_trips()
# always calls candidates(conn) with no `now`, so it measures recency against
# the real wall clock. _one_trip_now builds a trip against real time instead.

def _one_trip_now(conn, needs_review=False, minutes_ago=5,
                  duration_sec=300, heading="outbound"):
    seed_topology(conn)
    end = datetime.now(LOCAL_TZ) - timedelta(minutes=minutes_ago)
    start = end - timedelta(seconds=duration_sec)
    key = add_trip(conn, day=start.strftime("%Y-%m-%d"),
                   depart_mod=start.hour * 60 + start.minute,
                   duration_sec=duration_sec, heading=heading,
                   needs_review=needs_review)
    conn.commit()
    derive()
    return key


def _notification_row(conn, key):
    return conn.execute(
        "SELECT considered_at, sent_at, verdict FROM trip_notifications "
        "WHERE start_event_key=?", (key,)).fetchone()


def test_one_eligible_trip_produces_one_send_and_one_recorded_row(conn, monkeypatch):
    key = _one_trip_now(conn)
    sent = []

    def fake_send(conn, type_key, title, body, extra=None):
        sent.append({"type_key": type_key, "extra": extra})
        return {"push": "sent"}

    monkeypatch.setattr(notify_mod, "send", fake_send)

    results = trip_notify.notify_completed_trips()

    assert len(sent) == 1
    assert sent[0]["type_key"] == "trip_completed"
    assert sent[0]["extra"] == {"trip_key": key}
    assert [r["start_event_key"] for r in results] == [key]
    assert results[0]["verdict"] == "sent"

    row = _notification_row(conn, key)
    assert row["verdict"] == "sent"
    assert row["sent_at"] is not None


def test_a_second_call_sends_nothing(conn, monkeypatch):
    """The claim recorded by the first call is what stops the second — this
    is candidates()'s existing dedupe, exercised here through the real
    orchestrator rather than by calling record() directly."""
    key = _one_trip_now(conn)
    sent = []

    def fake_send(conn, type_key, title, body, extra=None):
        sent.append(extra)
        return {"push": "sent"}

    monkeypatch.setattr(notify_mod, "send", fake_send)

    first = trip_notify.notify_completed_trips()
    assert len(first) == 1
    assert len(sent) == 1

    second = trip_notify.notify_completed_trips()
    assert second == []
    assert len(sent) == 1, "a trip already considered must not be sent again"


def test_a_lost_claim_sends_nothing(conn, monkeypatch):
    """R1: `if not won: continue` is what makes a losing process skip a trip
    entirely — it is what closes the concurrent-push race, not
    test_claim_is_exclusive (which only tests record() in isolation) or
    test_a_second_call_sends_nothing above (which passes on candidates()'s
    older row-presence dedupe and would keep passing with the claim check
    deleted). Stubbing record() to report a lost claim is how the reviewer
    reached this branch in-process; the only other way in is genuine
    concurrency across two real OS processes."""
    key = _one_trip_now(conn)
    sent = []

    def fake_record(conn, start_event_key, verdict=None):
        return False  # every claim loses

    def fake_send(conn, type_key, title, body, extra=None):
        sent.append(extra)
        return {"push": "sent"}

    monkeypatch.setattr(trip_notify, "record", fake_record)
    monkeypatch.setattr(notify_mod, "send", fake_send)

    results = trip_notify.notify_completed_trips()

    assert sent == [], "a lost claim must never reach compose()/send()"
    assert results == []


def test_a_send_failure_is_recorded_and_does_not_abort_a_second_trip(conn, monkeypatch):
    """I6: compose(), send() and record() used to sit outside the enclosing
    try in a way that let a fault AFTER a push went out abort the loop before
    the remaining trips were considered. With claim-first this can no longer
    produce a duplicate, but the batch must still be resilient: one trip's
    send blowing up must not stop a second, independent trip in the same
    batch from being sent and recorded."""
    # Non-overlapping in real wall-clock time: derive()'s pairing logic
    # would otherwise interleave the two trips' geofence events.
    boom_key = _one_trip_now(conn, minutes_ago=20, heading="outbound")
    ok_key = _one_trip_now(conn, minutes_ago=6, heading="inbound")
    attempted = []

    def fake_send(conn, type_key, title, body, extra=None):
        tk = extra["trip_key"]
        attempted.append(tk)
        if tk == boom_key:
            raise RuntimeError("apns down")
        return {"push": "sent"}

    monkeypatch.setattr(notify_mod, "send", fake_send)

    results = trip_notify.notify_completed_trips()

    assert set(attempted) == {boom_key, ok_key}, "both trips must be attempted"
    by_key = {r["start_event_key"]: r["verdict"] for r in results}
    assert by_key[boom_key] == "failed"
    assert by_key[ok_key] == "sent"

    assert _notification_row(conn, boom_key)["verdict"] == "failed"
    assert _notification_row(conn, ok_key)["verdict"] == "sent"


def test_a_disabled_type_records_without_sending(conn):
    """Real notify.send() dispatch, not a stub: trip_completed's push channel
    is turned off, so send() itself reports 'disabled' without ever reaching
    push.send_alert (notify.py's enabled() gate short-circuits before the
    push import) — and notify_completed_trips() must still record that
    verdict rather than leaving the trip unconsidered."""
    key = _one_trip_now(conn)
    set_setting(conn, "notify_trip_completed_push", "0")
    conn.commit()

    results = trip_notify.notify_completed_trips()

    assert [r["start_event_key"] for r in results] == [key]
    assert results[0]["verdict"] == "disabled"

    row = _notification_row(conn, key)
    assert row["verdict"] == "disabled"
    assert row["sent_at"] is None
