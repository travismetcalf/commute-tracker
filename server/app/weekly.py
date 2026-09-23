"""Weekly commute summary — Monday to Friday of the week just ending.

MON-FRI, NOT MON-SUN, and stated deliberately: the job runs on a Saturday
morning, when the current week's Sunday has not happened yet. Reporting the
previous complete Mon-Sun week instead would be whole but stale on arrival. Every
other aggregate in this codebase is weekday-only anyway (stats.py filters
local_dow BETWEEN 0 AND 4 throughout) — this is a commute tracker.

LEFT JOIN routes everywhere, never an inner join. A trip can be status='complete'
with route_id NULL when derive could not resolve direction (derive.py:262); if a
human then confirms it, COUNTS_TOWARD_AVERAGES admits it. An inner join drops
exactly those rows, so the headline count and the per-direction blocks stop
reconciling with no error anywhere. Measured during design: 6 trips / 13980s
became 5 rows / 11180s.

Every figure is 'time on the tracked commute', not 'time in the car':
COUNTS_TOWARD_AVERAGES excludes status='non_commute' — real drives that are not
on a corridor this project watches. The copy says so rather than overclaiming.

Reads stored rows only. It must never issue a fresh TomTom call.
"""

from datetime import datetime, timedelta

from app.brief import fmt_min
from app.db import LOCAL_TZ
from app.stats import COUNTS_TOWARD_AVERAGES, _bucket_stats

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def week_range(now=None) -> tuple[str, str]:
    """(monday, friday) as YYYY-MM-DD for the week containing `now`.

    Resolved in LOCAL_TZ in Python, never with SQLite's localtime — the same
    rule db.py:6-9 states for every other local-time field here.
    """
    now = now or datetime.now(LOCAL_TZ)
    d = now.date()
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat(), (monday + timedelta(days=4)).isoformat()


_ROWS = f"""
    SELECT t.local_date, t.local_dow, t.duration_sec, t.start_utc,
           r.heading AS heading
      FROM trips t
      LEFT JOIN routes r ON t.route_id = r.id
     WHERE {COUNTS_TOWARD_AVERAGES}
       AND t.duration_sec IS NOT NULL
       AND t.local_dow BETWEEN 0 AND 4
       AND t.local_date BETWEEN ? AND ?
     ORDER BY t.start_utc
"""


def gather(conn, monday: str, friday: str) -> dict:
    """Every number the summary needs, from one query.

    One pass rather than six means the blocks cannot disagree with each other —
    which is the failure the LEFT JOIN note above describes.
    """
    rows = [dict(r) for r in conn.execute(_ROWS, (monday, friday))]

    directions: dict = {}
    for row in rows:
        key = row["heading"] or "unknown"
        directions.setdefault(key, []).append(row)

    per_dir = {}
    for key, trips in directions.items():
        ordered = sorted(trips, key=lambda t: (t["duration_sec"], t["start_utc"]))
        per_dir[key] = {
            **_bucket_stats([t["duration_sec"] for t in trips]),
            "total_sec": sum(t["duration_sec"] for t in trips),
            "best": ordered[0],
            "worst": ordered[-1],
        }

    by_day = {}
    for row in rows:
        by_day.setdefault(row["local_dow"], []).append(row)
    days = []
    for dow in range(5):
        trips = by_day.get(dow, [])
        days.append({
            "dow": dow,
            "name": DAY_NAMES[dow],
            "n": len(trips),
            "total_sec": sum(t["duration_sec"] for t in trips),
        })

    return {
        "monday": monday, "friday": friday,
        "n": len(rows),
        "total_sec": sum(r["duration_sec"] for r in rows),
        "directions": per_dir,
        "days": days,
    }


def _label(heading: str) -> str:
    return {"outbound": "Out", "inbound": "Home"}.get(heading, "Unknown")


def _esc(value) -> str:
    """Escape anything user-entered before it reaches the HTML body.

    Nothing rendered by this module is user-controlled today — dest_label
    (a /settings place label) was the one candidate, and gather() no longer
    selects it, so every current call site here passes a day name, a heading
    label, or a formatted duration. _esc is applied anyway, and stays applied
    at every interpolation, so that the first value which IS user-controlled
    cannot arrive unescaped by default — the repo is public, so someone
    else's deployment is someone else's threat model.
    """
    from html import escape
    return escape(str(value), quote=True)


def _span(monday: str, friday: str) -> str:
    m = datetime.strptime(monday, "%Y-%m-%d")
    f = datetime.strptime(friday, "%Y-%m-%d")
    return f"{m.strftime('%b %-d')} to {f.strftime('%b %-d')}"


def summary_text(conn, monday: str, friday: str) -> str | None:
    """Plain-text body, or None when the week had no qualifying trips."""
    d = gather(conn, monday, friday)
    if not d["n"]:
        return None
    lines = [f"Commute week — {_span(monday, friday)}", "",
             f"{d['n']} trips · {fmt_min(d['total_sec'])} on the tracked corridors",
             ""]
    for heading, s in sorted(d["directions"].items()):
        lines.append(f"{_label(heading):6s} avg {fmt_min(s['avg_sec'])}"
                     f"  (n={s['n']})")
        lines.append(f"       best  {DAY_NAMES[s['best']['local_dow']]}"
                     f" {fmt_min(s['best']['duration_sec'])}")
        lines.append(f"       worst {DAY_NAMES[s['worst']['local_dow']]}"
                     f" {fmt_min(s['worst']['duration_sec'])}")
    lines.append("")
    for day in d["days"]:
        total = fmt_min(day["total_sec"]) if day["n"] else "—"
        lines.append(f"{day['name']}  {day['n']} trips  {total}")
    return "\n".join(lines)


def summary_html(conn, monday: str, friday: str) -> str:
    """HTML body. Inline styles only — every mail client strips <style>."""
    d = gather(conn, monday, friday)
    if not d["n"]:
        return ""
    ink, faint, rule = "#1a1b2e", "#635f7d", "#e0dbd0"
    rows = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:{faint}">{_esc(day["name"])}</td>'
        f'<td style="padding:4px 12px 4px 0;text-align:right">{day["n"]}</td>'
        f'<td style="padding:4px 0;text-align:right">'
        f'{fmt_min(day["total_sec"]) if day["n"] else "—"}</td></tr>'
        for day in d["days"])
    dirs = "".join(
        f'<p style="margin:6px 0"><strong>{_esc(_label(h))}</strong> '
        f'avg {_esc(fmt_min(s["avg_sec"]))} '
        f'<span style="color:{faint}">(n={s["n"]})</span><br>'
        f'<span style="color:{faint}">best {_esc(DAY_NAMES[s["best"]["local_dow"]])} '
        f'{_esc(fmt_min(s["best"]["duration_sec"]))} · '
        f'worst {_esc(DAY_NAMES[s["worst"]["local_dow"]])} '
        f'{_esc(fmt_min(s["worst"]["duration_sec"]))}</span></p>'
        for h, s in sorted(d["directions"].items()))
    return (
        f'<div style="font-family:-apple-system,system-ui,sans-serif;color:{ink};'
        f'max-width:480px">'
        f'<h2 style="font-size:18px;margin:0 0 2px">Commute week</h2>'
        f'<p style="margin:0 0 14px;color:{faint};font-size:13px">'
        f'{_esc(_span(monday, friday))}</p>'
        f'<p style="margin:0 0 14px;font-size:15px">{d["n"]} trips · '
        f'<strong>{_esc(fmt_min(d["total_sec"]))}</strong> '
        f'on the tracked corridors</p>'
        f'{dirs}'
        f'<table style="border-collapse:collapse;margin-top:14px;font-size:13px;'
        f'border-top:1px solid {rule}">{rows}</table></div>')
