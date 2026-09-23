"""Aggregation layer. Every function returns `n` beside every value — the UI's
confidence tiers (n<5 dimmed / 5-14 labeled / 15+ full) depend on it.

Rules enforced here, not in templates:
- Weekdays only (local_dow 0-4), window local_hour 7-18 (ends AT 19:00).
- kind='live' preferred; fall back to kind='historical' (the departAt backfill)
  when a bucket is thin, and SAY SO via the 'source' field. Never mix kinds.
- p50/p90 computed in Python from sorted arrays — SQLite has no PERCENTILE and
  ~6k rows/quarter makes this microseconds.
- Which actual trips enter an average is defined ONCE, in
  COUNTS_TOWARD_AVERAGES. A flagged row waits for a human before it counts.
"""

from app.db import get_conn, get_setting, find_route

# "Leaving at 8 AM" is a ±15-min departure bucket, not the clock hour — 08:00
# and 08:55 traffic differ materially and averaging them answers nothing.
DEPART_WINDOW_MIN = 15
AM_DEPART_MOD = 8 * 60    # 08:00
PM_DEPART_MOD = 17 * 60   # 17:00

# A bucket needs this many live samples before live data is trusted over the
# historical profile.
LIVE_MIN_N = 5

WEEKDAY_HOURS = "local_dow BETWEEN 0 AND 4 AND local_hour BETWEEN 7 AND 18"


def _pct(sorted_vals: list, q: float):
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _bucket_stats(vals: list) -> dict:
    vals = sorted(vals)
    n = len(vals)
    return {
        "n": n,
        "avg_sec": round(sum(vals) / n) if n else None,
        "p10_sec": _pct(vals, 0.10),
        "p50_sec": _pct(vals, 0.50),
        "p90_sec": _pct(vals, 0.90),
        "min_sec": vals[0] if n else None,
        "max_sec": vals[-1] if n else None,
    }


def _grouped(conn, route_id: int, kind: str, bucket_expr: str, since_date: str | None) -> dict:
    """bucket -> [travel_sec,...] for weekday/window samples of one kind."""
    q = f"""SELECT {bucket_expr} AS bucket, travel_sec FROM predicted_samples
            WHERE route_id=? AND kind=? AND alt_index=0 AND {WEEKDAY_HOURS}"""
    params: list = [route_id, kind]
    if since_date and kind == "live":
        q += " AND local_date >= ?"
        params.append(since_date)
    out: dict = {}
    for row in conn.execute(q, params):
        out.setdefault(row["bucket"], []).append(row["travel_sec"])
    return out


def _merged_buckets(conn, route_id: int, bucket_expr: str, buckets: list,
                    since_date: str | None) -> list[dict]:
    """Per-bucket stats, live-first with historical fallback, source labeled."""
    live = _grouped(conn, route_id, "live", bucket_expr, since_date)
    hist = _grouped(conn, route_id, "historical", bucket_expr, None)
    out = []
    for b in buckets:
        lv, hv = live.get(b, []), hist.get(b, [])
        if len(lv) >= LIVE_MIN_N:
            row = {"bucket": b, "source": "live", **_bucket_stats(lv)}
        elif hv:
            row = {"bucket": b, "source": "historical", **_bucket_stats(hv)}
            row["live_n"] = len(lv)
        else:
            row = {"bucket": b, "source": "live", **_bucket_stats(lv)}
        out.append(row)
    return out


def hourly(conn, route_id: int, since_date: str | None = None) -> list[dict]:
    rows = _merged_buckets(conn, route_id, "local_hour", list(range(7, 19)), since_date)
    for r in rows:
        r["hour"] = r.pop("bucket")
    return rows


def dow(conn, route_id: int, since_date: str | None = None) -> list[dict]:
    rows = _merged_buckets(conn, route_id, "local_dow", list(range(0, 5)), since_date)
    for r in rows:
        r["dow"] = r.pop("bucket")
    return rows


def heatmap(conn, route_id: int, since_date: str | None = None) -> list[dict]:
    """dow x 30-min slot grid. bucket = dow*100 + slot30 keeps SQL simple."""
    rows = _merged_buckets(
        conn, route_id, "(local_dow*100)+(local_mod/30)",
        [d * 100 + s for d in range(5) for s in range(14, 38)],  # 07:00-18:30 slots
        since_date,
    )
    for r in rows:
        b = r.pop("bucket")
        r["dow"], r["slot30"] = b // 100, b % 100
    return rows


def depart_bucket(conn, route_id: int, center_mod: int,
                  since_date: str | None = None) -> dict:
    """Stats for the ±DEPART_WINDOW_MIN departure window around center_mod."""
    lo, hi = center_mod - DEPART_WINDOW_MIN, center_mod + DEPART_WINDOW_MIN

    def fetch(kind):
        q = """SELECT travel_sec, free_flow_sec FROM predicted_samples
               WHERE route_id=? AND kind=? AND alt_index=0
                 AND local_dow BETWEEN 0 AND 4 AND local_mod >= ? AND local_mod < ?"""
        params = [route_id, kind, lo, hi]
        if since_date and kind == "live":
            q += " AND local_date >= ?"
            params.append(since_date)
        return conn.execute(q, params).fetchall()

    live = fetch("live")
    if len(live) >= LIVE_MIN_N:
        rows, source = live, "live"
    else:
        hist = fetch("historical")
        rows, source = (hist, "historical") if hist else (live, "live")

    vals = [r["travel_sec"] for r in rows]
    ff = [r["free_flow_sec"] for r in rows if r["free_flow_sec"]]
    out = {"source": source, "live_n": len(live), **_bucket_stats(vals)}
    out["free_flow_sec"] = round(sum(ff) / len(ff)) if ff else None
    # 20-dot quantile dotplot (Kay et al., CHI 2016): invert the CDF at even
    # probabilities so "how often is it worse than X" reduces to counting dots.
    sv = sorted(vals)
    out["dots"] = [_pct(sv, i / 19) for i in range(20)] if sv else []
    return out


def headline(conn, since_date: str | None = None) -> dict:
    """The two big numbers + the baseline multiplier (the app's whole point).

    "The commute" is the PRIMARY corridor in each direction and "the baseline"
    is the BASELINE one — resolved from role+heading, because the slugs that
    used to stand in for them ('new-out', 'old-in') only ever existed in the
    deployment they were seeded for.
    """
    def bucket_for(route, mod):
        return depart_bucket(conn, route["id"], mod, since_date) if route else None

    primary_out = find_route(conn, "outbound")
    primary_in = find_route(conn, "inbound")
    base_out = find_route(conn, "outbound", role="baseline")
    base_in = find_route(conn, "inbound", role="baseline")

    new_out = bucket_for(primary_out, AM_DEPART_MOD)
    new_in = bucket_for(primary_in, PM_DEPART_MOD)
    old_out = bucket_for(base_out, AM_DEPART_MOD)
    old_in = bucket_for(base_in, PM_DEPART_MOD)

    def multiplier(new, old):
        if not new or not old or not new.get("avg_sec") or not old.get("avg_sec"):
            return None
        return {
            "ratio": round(new["avg_sec"] / old["avg_sec"], 1),
            "delta_sec": new["avg_sec"] - old["avg_sec"],
        }

    return {
        "am_out": new_out, "pm_in": new_in,
        "old_am_out": old_out, "old_pm_in": old_in,
        "am_multiplier": multiplier(new_out, old_out),
        "pm_multiplier": multiplier(new_in, old_in),
        # Whose numbers these are. The comparison is between two user-named
        # places now, so every surface that prints it is handed their labels
        # instead of spelling out one deployment's offices in its markup.
        "labels": {
            "am_out": primary_out["label"] if primary_out else None,
            "pm_in": primary_in["label"] if primary_in else None,
            "primary_place": _dest_label(conn, primary_out),
            "baseline_place": _dest_label(conn, base_out),
        },
    }


def _dest_label(conn, route) -> str | None:
    """The place a corridor ends at — "New Office", not "Home → New Office"."""
    if not route:
        return None
    row = conn.execute("SELECT label FROM places WHERE key=?",
                       (route["dest_key"],)).fetchone()
    return row["label"] if row else route["dest_key"]


def route_reliability(conn) -> dict:
    """Which corridor is the PREDICTABLE one, per enabled route.

    Corridor identity comes from driven length, not geometry hash — TomTom's
    polylines jitter cycle-to-cycle (40 hashes across 114 samples) and
    alt_index flip-flops between corridors as their live ranking changes.
    Distances cluster tightly per corridor and differ by >1.5 km between
    corridors, so a greedy gap-split on sorted lengths is identity enough.

    The score that matters for planning is the BUFFER (p90 - p50): the slack
    you must budget beyond the median. A corridor with a slower median but a
    tight buffer often beats a "faster" one you can't trust.
    """
    out = {}
    # Every enabled corridor, keyed by slug. Callers look themselves up by the
    # slug they are already rendering, so this no longer has to know which two
    # of them are "the" commute.
    routes = [(r["slug"], r["id"]) for r in conn.execute(
        "SELECT slug, id FROM routes WHERE enabled=1 ORDER BY id")]
    for slug, rid in routes:
        rows = conn.execute(
            """SELECT length_meters, travel_sec FROM predicted_samples
               WHERE route_id=? AND kind='live' AND length_meters IS NOT NULL
                 AND local_dow BETWEEN 0 AND 4 AND local_hour BETWEEN 7 AND 18
               ORDER BY length_meters""", (rid,)).fetchall()
        if not rows:
            out[slug] = {"corridors": [], "steadiest": None}
            continue
        clusters, cur = [], [rows[0]]
        for prev, nxt in zip(rows, rows[1:]):
            if nxt["length_meters"] - prev["length_meters"] > 1500:
                clusters.append(cur)
                cur = []
            cur.append(nxt)
        clusters.append(cur)

        corridors = []
        for c in clusters:
            vals = sorted(r["travel_sec"] for r in c)
            if len(vals) < 5:        # confidence floor — no derived claims
                continue
            lens = sorted(r["length_meters"] for r in c)
            st = _bucket_stats(vals)
            corridors.append({
                "mi": round(_pct(lens, 0.5) / 1609.34, 1),
                "n": len(vals),
                "p50_sec": st["p50_sec"],
                "p90_sec": st["p90_sec"],
                "buffer_sec": st["p90_sec"] - st["p50_sec"],
            })
        corridors.sort(key=lambda c: c["p50_sec"] or 0)
        eligible = [i for i, c in enumerate(corridors) if c["n"] >= 8]
        steadiest = (min(eligible, key=lambda i: corridors[i]["buffer_sec"])
                     if eligible else None)
        out[slug] = {"corridors": corridors, "steadiest": steadiest}
    return out


def cost(conn, since_date: str | None = None) -> dict:
    """Hours/week, hours/year, fuel $ — new office, with old-office deltas.
    Hybrid-aware: week/year math uses office days per week (x48 weeks), not a
    5-day assumption — the hybrid schedule is the biggest optimization there is."""
    mpg = float(get_setting(conn, "mpg", "28"))
    price = float(get_setting(conn, "fuel_price_per_gal", "3.60"))
    office_days = float(get_setting(conn, "office_days_per_week", "3"))
    days_year = round(office_days * 48)
    hl = headline(conn, since_date)

    def block(out_b, in_b):
        if not out_b or not in_b or not out_b.get("avg_sec") or not in_b.get("avg_sec"):
            return None
        day_sec = out_b["avg_sec"] + in_b["avg_sec"]
        hrs_week = day_sec * office_days / 3600
        hrs_year = day_sec * days_year / 3600
        return {"day_min": round(day_sec / 60), "hrs_week": round(hrs_week, 1),
                "hrs_year": round(hrs_year)}

    new = block(hl["am_out"], hl["pm_in"])
    old = block(hl["old_am_out"], hl["old_pm_in"])

    # Fuel: distance from the newest live sample's length per direction.
    def leg_meters(route):
        if not route:
            return None
        row = conn.execute(
            """SELECT length_meters FROM predicted_samples
               WHERE route_id=? AND alt_index=0 AND length_meters IS NOT NULL
               ORDER BY ts_utc DESC LIMIT 1""", (route["id"],)).fetchone()
        return row["length_meters"] if row else None

    def fuel(role):
        legs = [leg_meters(find_route(conn, h, role=role))
                for h in ("outbound", "inbound")]
        # A one-legged round trip would halve the bill and read as a saving.
        if not all(legs):
            return None
        return round(sum(legs) / 1609.34 / mpg * price * days_year)

    return {
        "new": new, "old": old,
        "fuel_year_new": fuel("primary"),
        "fuel_year_old": fuel("baseline"),
        "office_days_per_week": office_days,
        "days_per_year": days_year,
    }


#: Which trips may enter an average. Written once, used by every aggregate —
#: two copies of this predicate WILL drift, and the drift is invisible because
#: both still return plausible-looking numbers.
#:
#: 'complete' excludes noise/abandoned/non_commute. Not 'rejected' is the human
#: veto. The last clause is the subtle one: a row derive flagged for review and
#: nobody has adjudicated yet is a row we have said out loud we are unsure
#: about — an inferred destination, or a duration implausible against free-flow.
#: Counting it means the average silently depends on a guess, and an inferred
#: destination is wrong in one direction (a stop on the way home reads as a
#: short commute), so it biases rather than adding noise. Confirming pulls the
#: row straight in; the index page carries the outstanding count so the queue
#: cannot be forgotten.
COUNTS_TOWARD_AVERAGES = """t.status='complete' AND t.verdict != 'rejected'
             AND NOT (t.needs_review = 1 AND t.verdict = 'auto')"""


def actual_hourly(conn, route_id: int | None, heading: str) -> list[dict]:
    """Actual trip durations bucketed by hour, for the chart overlay."""
    q = f"""SELECT t.local_hour AS hour, t.duration_sec FROM trips t
           JOIN routes r ON t.route_id = r.id
           WHERE {COUNTS_TOWARD_AVERAGES}
             AND t.duration_sec IS NOT NULL AND r.heading = ?
             AND t.local_dow BETWEEN 0 AND 4"""
    params: list = [heading]
    if route_id:
        q += " AND t.route_id = ?"
        params.append(route_id)
    buckets: dict = {}
    for row in conn.execute(q, params):
        buckets.setdefault(row["hour"], []).append(row["duration_sec"])
    return [{"hour": h, **_bucket_stats(v)} for h, v in sorted(buckets.items())]


def trip_bucket(conn, route_id: int, local_mod: int,
                 exclude_key: str | None = None) -> dict:
    """Actual trip durations on one route in a ±DEPART_WINDOW_MIN departure
    window, weekdays pooled.

    Pooling Mon-Fri rather than splitting by day-of-week is deliberate: at two
    trips a day a per-dow bucket takes months to clear a usable sample, and the
    thing being answered is "is this drive worse than usual for this time of
    day", not "worse than other Thursdays".

    Bucketed over trips, not predicted_samples — depart_bucket() answers the
    prediction question and this answers the measured one. The predicate is
    COUNTS_TOWARD_AVERAGES, never a second copy of it.

    `exclude_key`, when given, drops that one start_event_key from the pool.
    A trip must not be counted in the average it is being compared against —
    comparing a measurement to a population that contains it is a measurement
    error, and it is worst exactly where the data is thinnest: at the
    MIN_SAMPLES floor the trip is a fifth of its own baseline, so an
    unusually bad commute would systematically under-report how unusual it
    was. Optional and defaulted so existing callers that aren't comparing a
    specific trip against the rest stay untouched.
    """
    lo, hi = local_mod - DEPART_WINDOW_MIN, local_mod + DEPART_WINDOW_MIN
    params = [route_id, lo, hi]
    exclude_clause = ""
    if exclude_key is not None:
        exclude_clause = "AND t.start_event_key != ?"
        params.append(exclude_key)
    vals = [r["duration_sec"] for r in conn.execute(
        f"""SELECT t.duration_sec FROM trips t
            WHERE {COUNTS_TOWARD_AVERAGES}
              AND t.duration_sec IS NOT NULL
              AND t.route_id = ?
              AND t.local_dow BETWEEN 0 AND 4
              AND t.local_mod BETWEEN ? AND ?
              {exclude_clause}""",
        params)]
    return _bucket_stats(vals)


def bias(conn) -> dict:
    """Actual vs predicted-at-that-moment: is TomTom optimistic about THIS commute?"""
    rows = conn.execute(
        f"""SELECT t.duration_sec, t.predicted_sec FROM trips t
           WHERE {COUNTS_TOWARD_AVERAGES}
             AND t.duration_sec IS NOT NULL AND t.predicted_sec IS NOT NULL"""
    ).fetchall()
    n = len(rows)
    if n == 0:
        return {"n": 0, "mean_actual_sec": None, "mean_predicted_sec": None,
                "mean_error_sec": None, "points": []}
    actual = [r["duration_sec"] for r in rows]
    pred = [r["predicted_sec"] for r in rows]
    return {
        "n": n,
        "mean_actual_sec": round(sum(actual) / n),
        "mean_predicted_sec": round(sum(pred) / n),
        "mean_error_sec": round(sum(a - p for a, p in zip(actual, pred)) / n),
        "points": [{"predicted_sec": p, "actual_sec": a} for a, p in zip(actual, pred)],
    }


def collection_health(conn) -> dict:
    """Is the collection still running? Counts only kind='live', alt_index=0 —
    alternates would inflate the count and the historical backfill answers a
    different question. MAX(ts_utc) is lexicographic-safe: the column is a
    fixed-width '%Y-%m-%dT%H:%M:%SZ' string."""
    from app.db import LOCAL_TZ
    from datetime import datetime as _dt
    row = conn.execute(
        """SELECT COUNT(*) AS n, MAX(ts_utc) AS newest FROM predicted_samples
           WHERE kind='live' AND alt_index=0""").fetchone()
    month = _dt.now(LOCAL_TZ).strftime("%Y-%m")
    usage = conn.execute("SELECT calls FROM api_usage WHERE month=?", (month,)).fetchone()
    # Whether we are INSIDE the polling window is decided here, in
    # America/New_York, not in the browser: a viewer in another timezone would
    # otherwise never see a dead poller flagged — the one thing this exists for.
    now = _dt.now(LOCAL_TZ)
    start = int(get_setting(conn, "window_start_hour", "7"))
    end = int(get_setting(conn, "window_end_hour", "19"))
    in_window = now.weekday() <= 4 and start <= now.hour < end
    return {
        "n_samples": row["n"] or 0,
        "newest_ts_utc": row["newest"],
        "api_calls": usage["calls"] if usage else 0,
        "api_cap": int(get_setting(conn, "monthly_call_cap", "18000")),
        "in_window": in_window,
    }


def _parse_hhmm(v: str, default_mod: int) -> int:
    try:
        h, m = v.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return default_mod


def plan_week(conn) -> dict:
    """Which office days, and when to leave each way.

    For every weekday, sweep home-departure times (15-min steps) inside the
    constraint chain: leave home >= earliest_leave_home; arrive after the
    outbound duration AT that hour; stay office_hours_per_day; leave the office
    no later than latest_leave_office; come home at THAT hour's inbound
    duration. Minimize the round trip. Rank the five weekdays; the best
    office_days_per_week are the recommendation.

    Data: p50 per (dow, 30-min slot), live-preferred with historical fallback —
    the same merged grid the heatmap renders.
    """
    earliest = _parse_hhmm(get_setting(conn, "earliest_leave_home", "07:00"), 420)
    latest = _parse_hhmm(get_setting(conn, "latest_leave_office", "18:00"), 1080)
    work_min = round(float(get_setting(conn, "office_hours_per_day", "8.0")) * 60)
    picks = max(1, min(5, round(float(get_setting(conn, "office_days_per_week", "3")))))
    window = {"earliest_mod": earliest, "latest_mod": latest,
              "office_hours": work_min / 60}

    # The plan chains one direction into the other, so it needs BOTH primary
    # corridors; with either missing there is no round trip to optimize.
    out_route, in_route = find_route(conn, "outbound"), find_route(conn, "inbound")
    if not out_route or not in_route:
        return {"days": [], "picks": picks, "window": window, "spread_sec": None}

    def grid(route_id):
        out = {}
        for c in heatmap(conn, route_id):
            if c["n"] and c["p50_sec"] is not None:
                out[(c["dow"], c["slot30"])] = (c["p50_sec"], c["source"])
        return out

    g_out, g_in = grid(out_route["id"]), grid(in_route["id"])

    days = []
    for dow in range(5):
        best = None
        for start in range(earliest, latest, 15):
            o = g_out.get((dow, start // 30))
            if not o:
                continue
            leave_office = start + o[0] / 60 + work_min
            if leave_office > latest:
                break  # later starts only push further past the cutoff
            i = g_in.get((dow, int(leave_office) // 30))
            if not i:
                continue
            total = o[0] + i[0]
            if best is None or total < best["total_sec"]:
                best = {
                    "dow": dow, "leave_home_mod": start,
                    "leave_office_mod": int(leave_office),
                    "out_sec": o[0], "in_sec": i[0], "total_sec": total,
                    "source": "live" if o[1] == i[1] == "live" else "historical",
                }
        if best:
            days.append(best)

    days.sort(key=lambda d: d["total_sec"])
    for rank, d in enumerate(days):
        d["pick"] = rank < picks
    worst = days[-1]["total_sec"] if days else None
    bestv = days[0]["total_sec"] if days else None
    return {
        "days": sorted(days, key=lambda d: d["dow"]),
        "picks": picks,
        "window": window,
        "spread_sec": (worst - bestv) if (worst is not None and bestv is not None) else None,
    }
