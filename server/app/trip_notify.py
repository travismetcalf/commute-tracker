"""Notify when a drive finishes.

Four guards earn their place here, and each exists because of something
measured rather than imagined:

  * RECENCY. derive() is also called from the manual-trip web form
    (main.py:282), which backfills old drives, and the phone replays a durable
    outbox that can carry a week of events at once. Without a recency gate both
    would announce drives finished days ago.
  * DEDUPE. derive() replays the entire event log every run and upserts every
    trip, and neither cursor.rowcount nor derived_at can distinguish a new row
    from a rewritten one. The trip_notifications side table is the only thing
    standing between this module and one push per historical trip every fifteen
    minutes.
  * REVIEW. status='complete' and needs_review=1 co-occur (derive.py:244 plus
    260/262/266). derive.py:39 is explicit that a confident wrong label is worse
    than an honest gap — a trip whose direction is unresolved must not announce
    "your trip home". candidates() uses stats.COUNTS_TOWARD_AVERAGES for the
    status/verdict half of this rather than a second hand-spelled copy of it —
    a hand-spelled near-copy is exactly how a rejected trip once became
    eligible again the instant a human rejected it (both adjudication
    endpoints clear needs_review=0 alongside the verdict). That predicate does
    NOT, on its own, keep a flagged trip ineligible once a human CONFIRMS it —
    confirming also sets needs_review=0, so every clause here passes and the
    trip is, briefly, a genuine candidate. What actually protects it is
    derive()'s ORDERING, not this query: the upsert re-derives needs_review
    from the event log on every run (derive.py:141) but never touches verdict
    (derive.py:148-149), so the very next derive() re-flags a still-unresolved
    trip back to needs_review=1 while verdict stays 'confirmed' — and both
    production callers run derive() immediately before calling
    notify_completed_trips(). AND t.needs_review = 0 below genuinely earns its
    place for exactly that resulting state (verdict='confirmed',
    needs_review=1); it is what derive's re-flag produces. Call this module
    standalone, without derive() running first, and that protection is gone.
  * CLAIM. Two processes call notify_completed_trips() concurrently in
    production: the container's /api/v1/ingest/events handler, on every
    ingest, and the host's derive-trips.sh, every 15 minutes. Nothing used to
    sit between candidates() and record() but compose() and a full APNs round
    trip — measured seconds wide with a real httpx.Client(timeout=10) per
    device and cold JWT signing — so both callers could see the same trip as
    a candidate and both push it. record(conn, key) with no verdict now claims
    the trip_notifications row FIRST, atomically (INSERT OR IGNORE against the
    PRIMARY KEY on start_event_key), and only the caller that wins the claim
    proceeds to compose() and send. See notify_completed_trips().

NOTHING HERE MAY RUN INSIDE AN OPEN WRITE TRANSACTION. push._send() opens its
own connection and WRITES to devices when APNs returns 410 (push.py:110), and a
nested write against a held write lock blocks for the full busy_timeout and then
raises 'database is locked' — measured, 10.47s. notify_completed_trips() opens
its own short connections and must be called only after any derive transaction
has committed.
"""

from datetime import datetime, timedelta, timezone

from app.db import LOCAL_TZ, get_conn, utcnow
from app.stats import COUNTS_TOWARD_AVERAGES

#: How recently a trip must have ended to be worth announcing.
RECENT_WINDOW_MIN = 30

#: Samples required before the "vs the average" clause is trustworthy. Matches
#: stats.LIVE_MIN_N and the n<5-is-dimmed convention the UI already uses. Here
#: `n` comes from trip_bucket(..., exclude_key=...), which excludes the trip
#: itself, so this floor means 5 *other* trips in the bucket (6 total) — one
#: more than the UI's uncorrected whole-bucket count asks for.
MIN_SAMPLES = 5

_SELECT = f"""
    SELECT t.start_event_key, t.duration_sec, t.predicted_sec, t.route_id,
           t.local_mod, t.end_utc,
           pd.kind  AS dest_kind,
           COALESCE(pd.label, t.dest_key) AS dest_label
      FROM trips t
      LEFT JOIN places pd ON t.dest_key  = pd.key
     WHERE {COUNTS_TOWARD_AVERAGES}
       AND t.needs_review = 0
       AND t.duration_sec IS NOT NULL
       AND t.end_utc IS NOT NULL
       AND t.start_event_key NOT IN (SELECT start_event_key FROM trip_notifications)
"""


def candidates(conn, now=None) -> list[dict]:
    """Trips that finished recently, are unflagged, and have not been considered.

    LEFT JOIN throughout: a complete trip can legitimately have route_id NULL
    (derive.py:262, direction unresolved), and an inner join would drop it
    silently rather than letting the caller decide.
    """
    now = now or datetime.now(LOCAL_TZ)
    cutoff = now - timedelta(minutes=RECENT_WINDOW_MIN)
    out = []
    for row in conn.execute(_SELECT):
        end = datetime.fromisoformat(row["end_utc"])
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if cutoff <= end <= now + timedelta(minutes=1):
            out.append(dict(row))
    return out


def compose(conn, trip: dict) -> tuple[str, str]:
    """(title, body). Every clause is dropped rather than faked when its number
    is missing — the message degrades, it never invents.

    Body wording matches what the user actually asked for, verbatim: "Your
    trip home took 58 minutes. 6 minutes longer than anticipated and 4
    minutes over the average" — full word "minutes", clauses joined with
    "and" rather than a comma. (Title keeps the terser "58 min" — see below,
    and the title/body split itself is unchanged: that split is how iOS
    renders a notification, and collapsing the two would be worse, not
    better.)"""
    from app.brief import fmt_min
    from app.stats import trip_bucket

    where = "home" if trip["dest_kind"] == "home" else f"to {trip['dest_label']}"
    title = f"Trip {where} · {fmt_min(trip['duration_sec'])}"

    parts = []
    if trip["predicted_sec"] is not None:
        delta = round((trip["duration_sec"] - trip["predicted_sec"]) / 60)
        if delta:
            word = "longer" if delta > 0 else "shorter"
            parts.append(f"{abs(delta)} minutes {word} than anticipated")
    if trip["route_id"] is not None:
        b = trip_bucket(conn, trip["route_id"], trip["local_mod"],
                        exclude_key=trip["start_event_key"])
        if b["n"] >= MIN_SAMPLES and b["avg_sec"] is not None:
            delta = round((trip["duration_sec"] - b["avg_sec"]) / 60)
            if delta:
                word = "over" if delta > 0 else "under"
                parts.append(f"{abs(delta)} minutes {word} the average")
    return title, " and ".join(parts)


def record(conn, start_event_key: str, verdict: str | None = None) -> bool:
    """Claim (verdict=None) or finalize (any other verdict) this trip's
    trip_notifications row. Returns whether THIS call owns the row.

    CLAIM: verdict=None does INSERT OR IGNORE against the PRIMARY KEY on
    start_event_key. That is what makes it atomic across the two concurrent
    callers of notify_completed_trips() described in the module docstring —
    of two racing claims for the same key, only one INSERT can land, so the
    return value tells the caller whether it won and may proceed to compose()
    and send, or lost and must skip the trip entirely.

    FINALIZE: any other verdict UPDATEs the row this process already won the
    claim on — never INSERT OR IGNORE, which would silently no-op against the
    row the claim step already inserted and leave the real verdict
    unrecorded. Written whether or not anything was actually sent, so
    enabling the switch later does not deliver a backlog.
    """
    now = utcnow()
    if verdict is None:
        cur = conn.execute(
            """INSERT OR IGNORE INTO trip_notifications
                 (start_event_key, considered_at, sent_at, verdict)
               VALUES (?, ?, NULL, NULL)""",
            (start_event_key, now))
        return cur.rowcount > 0
    cur = conn.execute(
        """UPDATE trip_notifications SET sent_at=?, verdict=?
            WHERE start_event_key=?""",
        (now if verdict == "sent" else None, verdict, start_event_key))
    return cur.rowcount > 0


def notify_completed_trips() -> list[dict]:
    """Announce every eligible trip. Returns one dict per trip THIS call
    actually claimed and considered — a trip a concurrent caller already
    claimed (see CLAIM in the module docstring) is skipped silently and does
    not appear here.

    MUST NOT be called inside an open write transaction — see the module
    docstring. Each trip gets its own short connection so the APNs round trip
    never happens with a write lock held.

    Taking the claim before sending (see CLAIM in the module docstring) is
    what makes delivery AT-MOST-ONCE rather than at-least-once: two
    concurrent callers can no longer both announce the same trip, but the
    price is that a claimed row is a permanent tombstone — candidates()
    excludes on row PRESENCE, not on verdict, so a fault anywhere after the
    claim lands (compose() raising, the finalize write failing, or send()
    itself raising) permanently drops that one notification rather than
    retrying it on a later run. This is deliberate: for a push notification a
    duplicate announcement is worse than a missed one. It is still a real
    narrowing versus the pre-claim behaviour, though, and worth naming
    plainly — a compose() fault used to leave the trip unclaimed, so it
    retried on the next run; now it does not. compose() reads the database
    via trip_bucket, and this deployment runs two writers sharing a
    10-second busy_timeout, so 'database is locked' is the realistic
    trigger for that fault here, not a hypothetical one.

    The whole per-trip body below runs inside one try: a fault in the claim,
    compose, or finalize writes for one trip must not abort the trips after
    it in this batch. The inner try around send() alone is narrower — it
    exists so a delivery fault still reaches the finalize write (recording
    'failed') rather than leaving the trip's already-claimed row stuck at
    verdict=NULL. That finalize write is NOT duplicate suppression: the row
    was already claimed before send() ran, and candidates() excludes on row
    PRESENCE alone (see the at-most-once paragraph above) — this trip would
    never be reconsidered either way, verdict=NULL or 'failed'. The inner
    try's real benefit is an accurate log line: 'failed' tells the next
    reader the push was attempted and lost, instead of a bare NULL that
    looks identical to a claim whose process died before send() ever ran.
    """
    from app import notify

    with get_conn() as conn:
        pending = candidates(conn)
    results = []
    for trip in pending:
        key = trip["start_event_key"]
        try:
            with get_conn() as conn:
                won = record(conn, key)
            if not won:
                continue  # another process already claimed this trip
            with get_conn() as conn:
                title, body = compose(conn, trip)
            try:
                with get_conn() as conn:
                    verdict = notify.send(conn, "trip_completed", title, body,
                                          extra={"trip_key": key})
                v = verdict.get("push", "unknown")
            except Exception as exc:            # noqa: BLE001 — see above
                print(f"trip_notify: send {type(exc).__name__}: {exc}", flush=True)
                v = "failed"
            with get_conn() as conn:
                record(conn, key, v)
            results.append({"start_event_key": key, "title": title, "verdict": v})
        except Exception as exc:                # noqa: BLE001 — see above
            print(f"trip_notify: {type(exc).__name__}: {exc}", flush=True)
    return results
