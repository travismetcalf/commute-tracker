"""Daily brief + anomaly detection.

Brief (07:15 weekdays via brief.sh): today's picture for the primary outbound
route (resolved by role+heading, whatever the user named its endpoints) —
current predicted time, the best departure window in the next 3 hours, and the
delta against leaving at 8:00.

Anomaly (rides every poll via anomaly-check.py): newest live sample vs the
trailing p90 for its (route, dow, 30-min slot). Alerts only when BOTH
  - sample > p90, and
  - sample > median + anomaly_min_delta_min
and only after anomaly_min_live_days of live data exist (an alert against the
backfill or n=4 is noise). At most one alert per route per day, tracked in
scripts/anomaly-state.
"""

from datetime import datetime

from app.db import get_conn, get_setting, find_route, LOCAL_TZ
from app.stats import depart_bucket, _bucket_stats, AM_DEPART_MOD


def fmt_min(sec) -> str:
    if sec is None:
        return "—"
    m = round(sec / 60)
    return f"{m // 60} h {m % 60} m" if m > 99 else f"{m} min"


def brief_text() -> str | None:
    """Plain-text morning brief, or None when there's nothing useful to say."""
    now = datetime.now(LOCAL_TZ)
    with get_conn() as conn:
        route = find_route(conn, "outbound")
        if not route:
            return None
        destination = conn.execute(
            "SELECT label FROM places WHERE key=?", (route["dest_key"],)).fetchone()
        going_to = destination["label"] if destination else "the office"

        newest = conn.execute(
            """SELECT travel_sec, historic_sec, ts_utc FROM predicted_samples
               WHERE route_id=? AND kind='live' AND alt_index=0
               ORDER BY ts_utc DESC LIMIT 1""",
            (route["id"],),
        ).fetchone()

        # Best 30-min departure window in the next 3 hours, from bucket stats.
        now_mod = now.hour * 60 + now.minute
        candidates = []
        for slot_start in range(now_mod - now_mod % 30, now_mod + 180, 30):
            if slot_start < 7 * 60 or slot_start >= 19 * 60:
                continue
            b = depart_bucket(conn, route["id"], slot_start + 15)
            if b["avg_sec"]:
                candidates.append((slot_start, b))
        eight = depart_bucket(conn, route["id"], AM_DEPART_MOD)

    lines = [f"Commute brief — {now.strftime('%A %B %-d')}"]
    if newest:
        lines.append(f"Right now: {fmt_min(newest['travel_sec'])} to {going_to}"
                     + (f" (typical {fmt_min(newest['historic_sec'])})" if newest["historic_sec"] else ""))
    if candidates:
        best_slot, best = min(candidates, key=lambda x: x[1]["avg_sec"])
        hh, mm = best_slot // 60, best_slot % 60
        lines.append(f"Best window (next 3 h): leave ~{hh}:{mm:02d} → {fmt_min(best['avg_sec'])}"
                     f" [{best['source']}, n={best['n']}]")
    if eight and eight.get("avg_sec"):
        lines.append(f"Leaving at 8:00 typically runs {fmt_min(eight['avg_sec'])}"
                     f" (p90 {fmt_min(eight['p90_sec'])})")
    return "\n".join(lines) if len(lines) > 1 else None


ALERT_DEFAULTS = {
    "alert_days": "0,1,2,3,4",      # 0=Mon .. 6=Sun
    "alert_am_start": "06:30", "alert_am_end": "10:00",
    "alert_pm_start": "15:30", "alert_pm_end": "19:00",
}


def _alert_window_active(conn, now=None) -> bool:
    """Anomaly ALERTS only fire inside the user's commute windows (the silent
    widget-refresh push is unaffected — it has no user-facing surface)."""
    from app.stats import _parse_hhmm
    now = now or datetime.now(LOCAL_TZ)
    days = get_setting(conn, "alert_days", ALERT_DEFAULTS["alert_days"])
    try:
        active_days = {int(d) for d in days.split(",") if d.strip() != ""}
    except ValueError:
        active_days = {0, 1, 2, 3, 4}
    if now.weekday() not in active_days:
        return False
    mod = now.hour * 60 + now.minute
    am = (_parse_hhmm(get_setting(conn, "alert_am_start", ALERT_DEFAULTS["alert_am_start"]), 390),
          _parse_hhmm(get_setting(conn, "alert_am_end", ALERT_DEFAULTS["alert_am_end"]), 600))
    pm = (_parse_hhmm(get_setting(conn, "alert_pm_start", ALERT_DEFAULTS["alert_pm_start"]), 930),
          _parse_hhmm(get_setting(conn, "alert_pm_end", ALERT_DEFAULTS["alert_pm_end"]), 1140))
    return am[0] <= mod < am[1] or pm[0] <= mod < pm[1]


def check_anomaly() -> list[dict]:
    """Return [{route_slug, sample_min, p90_min, median_min}] for alert-worthy samples."""
    out = []
    with get_conn() as conn:
        if not _alert_window_active(conn):
            return []
        min_days = int(get_setting(conn, "anomaly_min_live_days", "21"))
        min_delta = int(get_setting(conn, "anomaly_min_delta_min", "10")) * 60

        live_days = conn.execute(
            "SELECT COUNT(DISTINCT local_date) AS n FROM predicted_samples WHERE kind='live'"
        ).fetchone()["n"]
        if live_days < min_days:
            return []

        for r in conn.execute("SELECT * FROM routes WHERE enabled=1 AND role='primary'"):
            newest = conn.execute(
                """SELECT * FROM predicted_samples
                   WHERE route_id=? AND kind='live' AND alt_index=0
                   ORDER BY ts_utc DESC LIMIT 1""",
                (r["id"],),
            ).fetchone()
            if not newest:
                continue
            slot30 = newest["local_mod"] // 30
            vals = [row["travel_sec"] for row in conn.execute(
                """SELECT travel_sec FROM predicted_samples
                   WHERE route_id=? AND kind='live' AND alt_index=0
                     AND local_dow=? AND local_mod/30=? AND id != ?""",
                (r["id"], newest["local_dow"], slot30, newest["id"]))]
            if len(vals) < 8:
                continue
            st = _bucket_stats(vals)
            if (newest["travel_sec"] > st["p90_sec"]
                    and newest["travel_sec"] > st["p50_sec"] + min_delta):
                out.append({
                    "route_slug": r["slug"],
                    "sample_min": round(newest["travel_sec"] / 60),
                    "p90_min": round(st["p90_sec"] / 60),
                    "median_min": round(st["p50_sec"] / 60),
                })
    return out
