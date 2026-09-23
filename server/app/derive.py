"""Event → trip pairing. Runs every 15 min (derive-trips.sh) and on demand.

trip_events is append-only truth; trips is a derived view. Everything here is
idempotent (upsert keyed on start_event_key), and --rebuild-all truncates trips
and replays the whole log — the reason a pairing bug can never destroy data.

Two signals report the same drive and they fail in OPPOSITE directions, so both
are kept and each is used for what it is good at:

  * Bluetooth/Shortcuts intents (intent_assert/drive_end) are RELIABLE but
    imprecise — the stereo pairs before you move and disconnects after you park,
    bracketing the drive by a minute or two at each end. They carry no place.
  * Geofence crossings are PRECISE but unreliable — the boundary is the true
    departure/arrival, when the OS bothers to report it. They carry the place.

So intents decide THAT a trip happened, geofences decide WHERE and WHEN. Neither
is required: a deployment with no Shortcuts automations pairs purely on
geofences (rule 2), and one whose geofences never fire still gets trips, just
with unresolved endpoints (rule 8).

Pairing rules (in order):
1. drive_start/intent_assert opens a trip; drive_end closes the most recent open.
2. geofence_leave(place) also opens (when no drive_start arrived — geofence-only
   mode); geofence_enter(place) closes and resolves the destination.
3. A geofence_leave arriving just behind an intent-opened trip is the SAME
   departure seen more precisely: it pins the origin and the true start time.
   The trip's identity (start_event_key) stays the opening event, so the phone's
   track upload — which keys on the event that opened its recording — still
   matches, and manual verdicts survive a rebuild.
4. A start and end < 90s apart is 'noise' (moving the car in the driveway).
5. An end with no open start within MAX_TRIP_H is dropped as noise.
6. An open trip older than MAX_TRIP_H becomes 'abandoned', duration NULL,
   needs_review — it must never surface as a bogus 4-hour commute.
7. Both endpoints observed, but the same place or with no tracked route between
   them, is 'non_commute' — a real drive, just not one this project measures.
   Kept visible (a silently dropped row is one you never notice misfiring) and
   excluded from every average by STATUS, so `verdict` stays purely human.
8. Exactly one endpoint observed, and exactly one route leaves/reaches it →
   infer the other, flagged needs_review. Otherwise the direction stays NULL and
   is flagged. Never guessed from the clock: a confident wrong label is worse
   than an honest gap, and it silently poisons the averages it feeds.
9. Plausibility vs the route's median free-flow: flag, never discard.
10. Nearest live predicted sample within ±10 min of start → predicted_sec.

Nothing here knows a place key or a schedule — routes and places are read from
the DB, so a deployment with different endpoints, corridors and hours behaves
correctly with no code change.
"""

from datetime import datetime, timezone

from app.db import get_conn, local_fields, utcnow

NOISE_UNDER_SEC = 90
MAX_TRIP_H = 4
PREDICT_MATCH_MIN = 10
# How long after an intent-opened trip a geofence crossing still counts as the
# same departure. Observed lag is 1-2 min (stereo pairs, then you pull out);
# 5 min is slack for a slow fix without reaching the next real trip.
REFINE_WINDOW_SEC = 300

START_KINDS = {"drive_start", "intent_assert", "geofence_leave", "manual"}
END_KINDS = {"drive_end", "geofence_enter"}
# Openers that carry no place and so can be refined by a following crossing.
PLACELESS_START_KINDS = {"drive_start", "intent_assert", "manual"}


def _dt(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _iso(epoch: int) -> str:
    return _dt(epoch).strftime("%Y-%m-%dT%H:%M:%SZ")


def _routes_matching(conn, origin_key=None, dest_key=None):
    """Enabled, non-alternate routes with the given endpoint(s). One helper so
    'is there a route between these?' and 'what could this trip be?' can never
    disagree about which routes count — a mismatch there would classify a trip
    non_commute and then look for a route it had already excluded."""
    where, params = ["enabled=1", "role != 'alternate'"], []
    if origin_key:
        where.append("origin_key=?")
        params.append(origin_key)
    if dest_key:
        where.append("dest_key=?")
        params.append(dest_key)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM routes WHERE " + " AND ".join(where) + " ORDER BY id", params)]


def _route_for(conn, origin_key, dest_key):
    if not origin_key or not dest_key:
        return None
    rows = _routes_matching(conn, origin_key, dest_key)
    return rows[0]["id"] if rows else None


def _predicted(conn, route_id, start_epoch):
    if not route_id:
        return None, None
    iso = _iso(start_epoch)
    row = conn.execute(
        """SELECT id, travel_sec FROM predicted_samples
           WHERE route_id=? AND kind='live' AND alt_index=0
             AND ABS(strftime('%s', ts_utc) - ?) <= ?
           ORDER BY ABS(strftime('%s', ts_utc) - ?) LIMIT 1""",
        (route_id, start_epoch, PREDICT_MATCH_MIN * 60, start_epoch),
    ).fetchone()
    return (row["id"], row["travel_sec"]) if row else (None, None)


def _median_free_flow(conn, route_id):
    if not route_id:
        return None
    vals = [r["free_flow_sec"] for r in conn.execute(
        "SELECT free_flow_sec FROM predicted_samples WHERE route_id=? AND free_flow_sec IS NOT NULL",
        (route_id,))]
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def _upsert_trip(conn, trip: dict):
    conn.execute(
        """INSERT INTO trips (start_event_key, end_event_key, start_utc, end_utc,
             local_date, local_hour, local_mod, local_dow, origin_key, dest_key,
             route_id, duration_sec, predicted_sec, predicted_sample_id, status,
             needs_review, review_reason, derived_at)
           VALUES (:start_event_key, :end_event_key, :start_utc, :end_utc,
             :local_date, :local_hour, :local_mod, :local_dow, :origin_key, :dest_key,
             :route_id, :duration_sec, :predicted_sec, :predicted_sample_id, :status,
             :needs_review, :review_reason, :derived_at)
           ON CONFLICT(start_event_key) DO UPDATE SET
             end_event_key=excluded.end_event_key, end_utc=excluded.end_utc,
             origin_key=excluded.origin_key, dest_key=excluded.dest_key,
             route_id=excluded.route_id, duration_sec=excluded.duration_sec,
             predicted_sec=excluded.predicted_sec,
             predicted_sample_id=excluded.predicted_sample_id,
             status=excluded.status, needs_review=excluded.needs_review,
             review_reason=excluded.review_reason, derived_at=excluded.derived_at""",
        trip,
    )


def derive(rebuild: bool = False) -> dict:
    """Replay trip_events into trips. Manual verdicts survive non-rebuild runs
    because the upsert never touches the verdict column.

    REBUILD is the documented recovery tool ("a pairing bug can never destroy
    data"), so it must not destroy the two things that are NOT derivable from
    the event log: human verdicts, and the iOS-recorded polylines in
    trip_tracks (which FK-cascade off trips.id). Both are preserved across the
    DELETE by keying them to start_event_key — the one stable identity that
    survives a rebuild — and restored after the replay.
    """
    stats = {"complete": 0, "open": 0, "abandoned": 0, "noise": 0, "non_commute": 0}
    with get_conn() as conn:
        saved_verdicts, saved_tracks = [], []
        if rebuild:
            saved_verdicts = [dict(r) for r in conn.execute(
                """SELECT start_event_key, verdict, verdict_by FROM trips
                   WHERE verdict != 'auto'""")]
            saved_tracks = [dict(r) for r in conn.execute(
                """SELECT t.start_event_key, k.points_json, k.bbox_json,
                          k.length_meters, k.point_count
                   FROM trip_tracks k JOIN trips t ON k.trip_id = t.id""")]
            conn.execute("DELETE FROM trips")

        events = [dict(r) for r in conn.execute(
            """SELECT * FROM trip_events
               WHERE kind IN ('drive_start','drive_end','intent_assert',
                              'geofence_enter','geofence_leave','manual')
               ORDER BY device_ts, id""")]

        now_epoch = int(datetime.now(timezone.utc).timestamp())
        open_trip = None    # the event that opened the current trip
        open_refine = None  # geofence_leave pinning that trip's true departure

        def departure():
            """(start epoch, origin key) — the crossing when we have one, the
            opening event otherwise. Rule 3."""
            src = open_refine or open_trip
            return src["device_ts"], src.get("place_key")

        def close_open_as_abandoned():
            nonlocal open_trip, open_refine
            if open_trip is None:
                return
            start_ts, origin = departure()
            _upsert_trip(conn, {
                "start_event_key": open_trip["event_key"], "end_event_key": None,
                "start_utc": _iso(start_ts), "end_utc": None,
                **local_fields(_dt(start_ts)),
                "origin_key": origin, "dest_key": None,
                "route_id": None, "duration_sec": None,
                "predicted_sec": None, "predicted_sample_id": None,
                "status": "abandoned", "needs_review": 1,
                "review_reason": "no end event within %dh" % MAX_TRIP_H,
                "derived_at": utcnow(),
            })
            stats["abandoned"] += 1
            open_trip, open_refine = None, None

        for ev in events:
            if open_trip and ev["device_ts"] - open_trip["device_ts"] > MAX_TRIP_H * 3600:
                close_open_as_abandoned()

            if ev["kind"] in START_KINDS:
                if ev["kind"] == "geofence_leave" and open_trip:
                    # Already driving. If the crossing is close behind a
                    # placeless opener it is that same departure, seen exactly —
                    # keep it for the origin and the true start time (rule 3).
                    # Later crossings are a real trip's worth away and ignored,
                    # which is also what makes a duplicate double-fire harmless.
                    if (open_refine is None
                            and open_trip["kind"] in PLACELESS_START_KINDS
                            and ev["device_ts"] - open_trip["device_ts"] <= REFINE_WINDOW_SEC):
                        open_refine = ev
                    continue
                if open_trip and ev["kind"] in ("drive_start", "intent_assert"):
                    # duplicate start: keep the earlier one
                    continue
                open_trip, open_refine = ev, None

            elif ev["kind"] in END_KINDS:
                if not open_trip:
                    continue  # orphan end → ignore; raw event survives forever
                start_ts, origin = departure()
                dest = ev.get("place_key")
                dur = ev["device_ts"] - start_ts
                route_id, needs_review, reason = None, 0, None

                if dur < NOISE_UNDER_SEC:
                    status, reason = "noise", "under %ds" % NOISE_UNDER_SEC
                elif origin and dest and origin == dest:
                    # Left and came back: an errand, or a boundary the GPS
                    # dithered across. Either way not a corridor measurement.
                    status, reason = "non_commute", "returned to the same place"
                elif origin and dest and not _routes_matching(conn, origin, dest):
                    status, reason = "non_commute", "no tracked route between these places"
                else:
                    status = "complete"
                    if origin and dest:
                        # A pair with no route between them was classified
                        # non_commute above, so reaching here with both ends
                        # known means a route exists.
                        route_id = _route_for(conn, origin, dest)
                    else:
                        # Rule 8. One known end plus exactly one route touching
                        # it is arithmetic, not a guess; anything else stays NULL.
                        missing = "destination" if origin else "origin"
                        cands = (_routes_matching(conn, origin_key=origin) if origin
                                 else _routes_matching(conn, dest_key=dest) if dest
                                 else [])
                        if len(cands) == 1:
                            route_id = cands[0]["id"]
                            origin, dest = cands[0]["origin_key"], cands[0]["dest_key"]
                            needs_review, reason = 1, "%s inferred" % missing
                        else:
                            needs_review, reason = 1, "direction unresolved"

                    ff = _median_free_flow(conn, route_id)
                    if ff and (dur < max(300, 0.5 * ff) or dur > 2.5 * ff):
                        needs_review, reason = 1, "implausible vs free-flow"

                sample_id, pred = (None, None)
                if status == "complete":
                    sample_id, pred = _predicted(conn, route_id, start_ts)

                _upsert_trip(conn, {
                    "start_event_key": open_trip["event_key"], "end_event_key": ev["event_key"],
                    "start_utc": _iso(start_ts), "end_utc": _iso(ev["device_ts"]),
                    **local_fields(_dt(start_ts)),
                    "origin_key": origin, "dest_key": dest, "route_id": route_id,
                    # A non_commute is still a real measured drive — keep its
                    # duration so the row can be sanity-checked at a glance.
                    "duration_sec": dur if status in ("complete", "non_commute") else None,
                    "predicted_sec": pred, "predicted_sample_id": sample_id,
                    "status": status, "needs_review": needs_review,
                    "review_reason": reason, "derived_at": utcnow(),
                })
                stats[status] = stats.get(status, 0) + 1
                open_trip, open_refine = None, None

        # trailing open trip
        if open_trip:
            if now_epoch - open_trip["device_ts"] > MAX_TRIP_H * 3600:
                close_open_as_abandoned()
            else:
                start_ts, origin = departure()
                _upsert_trip(conn, {
                    "start_event_key": open_trip["event_key"], "end_event_key": None,
                    "start_utc": _iso(start_ts), "end_utc": None,
                    **local_fields(_dt(start_ts)),
                    "origin_key": origin, "dest_key": None,
                    "route_id": None, "duration_sec": None,
                    "predicted_sec": None, "predicted_sample_id": None,
                    "status": "open", "needs_review": 0, "review_reason": None,
                    "derived_at": utcnow(),
                })
                stats["open"] += 1

        # Restore what the replay cannot reconstruct. Rows whose start event no
        # longer produces a trip are simply dropped (the event was deleted or
        # re-paired) — counted so a rebuild can never lose data silently.
        if rebuild:
            stats["verdicts_restored"] = 0
            for v in saved_verdicts:
                stats["verdicts_restored"] += conn.execute(
                    "UPDATE trips SET verdict=?, verdict_by=? WHERE start_event_key=?",
                    (v["verdict"], v["verdict_by"], v["start_event_key"])).rowcount
            stats["tracks_restored"] = 0
            for t in saved_tracks:
                row = conn.execute("SELECT id FROM trips WHERE start_event_key=?",
                                   (t["start_event_key"],)).fetchone()
                if not row:
                    continue
                conn.execute(
                    """INSERT OR REPLACE INTO trip_tracks
                         (trip_id, points_json, bbox_json, length_meters, point_count)
                       VALUES (?, ?, ?, ?, ?)""",
                    (row["id"], t["points_json"], t["bbox_json"],
                     t["length_meters"], t["point_count"]))
                stats["tracks_restored"] += 1
            stats["verdicts_dropped"] = len(saved_verdicts) - stats["verdicts_restored"]
            stats["tracks_dropped"] = len(saved_tracks) - stats["tracks_restored"]
    return stats
