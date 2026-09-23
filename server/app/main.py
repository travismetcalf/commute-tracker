"""commute-tracker web app.

Auth model (two disjoint surfaces — never cross them):
- Pages + /api/stats/* + /tiles/*: authentik forward-auth at Caddy. The
  X-authentik-* headers are DISPLAY-ONLY identity — on the LAN surface they are
  absent (and spoofable), so nothing here makes an authorization decision from
  them. Every real gate is Caddy's.
- /api/v1/*: bearer token (iOS app + widget). Caddy skips forward_auth for
  these paths; require_bearer() enforces hmac.compare_digest on every one.

The TomTom key never reaches the browser: map tiles are proxied via /tiles/*
(base tiles disk-cached 30 days; flow tiles passed through with max-age=60).
"""

import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, Response, HTTPException, Form
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from app import stats as stats_mod
from app import brief as brief_mod
from app import notify as notify_mod
#: Event kinds that can close a trip (used by _should_derive, below). Imported
#: at module scope rather than function-locally: _should_derive is called from
#: ingest_events OUTSIDE the ingest transaction's try/except, so an import
#: fault here must surface at process startup, never turn an
#: already-committed ingest into a 500. `derive()` itself stays a
#: function-local import at its one call site — see the comment there.
from app.derive import END_KINDS
from app.db import (get_conn, init_db, local_fields, utcnow, get_setting,
                    set_setting, get_places, get_routes, find_route, LOCAL_TZ)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.dirname(os.environ.get("DB_PATH", "/app/data/commute.db"))
TILE_CACHE = os.path.join(DATA_DIR, "tilecache")

TOMTOM_API_KEY = os.environ.get("TOMTOM_API_KEY", "")
API_BEARER_TOKEN = os.environ.get("API_BEARER_TOKEN", "")

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Cache-bust every static asset by content mtime. WHY: StaticFiles sends only
# last-modified and etag, no cache-control, so a browser applies HEURISTIC
# freshness — commonly ~10% of the file's age — and simply does not revalidate.
# app.css had been unchanged since early August, so the settings redesign
# shipped to a browser that kept serving the pre-redesign stylesheet for weeks:
# the markup was the new one, none of the .setting/.switch/.matrix__ rules
# applied, and the page rendered as unstyled stacked text. A versioned URL is a
# different cache key, so a rebuild invalidates it the moment the file changes.
def _asset_version() -> str:
    """Newest mtime under static/, as a short hex stamp.

    Computed once at import: the files cannot change under a running container
    (the image is immutable and a deploy restarts the process), so re-walking
    the tree per request would buy nothing.
    """
    root = os.path.join(BASE_DIR, "static")
    newest = 0.0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, f)))
            except OSError:
                continue
    return format(int(newest), "x")


templates.env.globals["asset_v"] = _asset_version()

from app.viz import dotplot_svg, fmt_min, mins  # noqa: E402
templates.env.filters["fmt_min"] = fmt_min
templates.env.filters["mins"] = mins
templates.env.filters["dotplot"] = dotplot_svg


def hhmm(mod) -> str:
    """Minutes-since-midnight -> '7:15 AM'."""
    if mod is None:
        return "—"
    h, mi = int(mod) // 60, int(mod) % 60
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{mi:02d} {ap}"


templates.env.filters["hhmm"] = hhmm

VALID_EVENT_KINDS = {"geofence_enter", "geofence_leave", "drive_start", "drive_end",
                     "intent_assert", "motion_segment", "manual"}
VALID_SOURCES = {"ios", "intent", "motion_backfill", "web_manual"}

# Out-of-window live-route guard: in-process TTL cache + daily counter so a
# forgotten browser tab can never burn the TomTom budget (2,880 calls/day at a
# 30s refresh would eat the whole monthly cap in a week).
_route_cache: dict = {}   # route_id -> (fetched_epoch, payload)
_live_calls = {"date": "", "n": 0}
LIVE_TTL_SEC = 300
LIVE_MAX_PER_DAY = 60

# The map draws corridor 0..2, so the NOW card must compare exactly those — a
# corridor listed with a time but absent from the map has no line to point at.
ALT_SLOTS = 3
# Past this age a sample is no longer "live". Shared so the label on the line
# and the row in the card can never disagree about which corridors are fresh.
STALE_AFTER_SEC = 15 * 60


@app.on_event("startup")
def _startup():
    os.makedirs(TILE_CACHE, exist_ok=True)
    init_db()


# ── identity (display only) ──────────────────────────────────────────────────

def current_user(request: Request) -> dict:
    username = request.headers.get("X-authentik-username")
    return {
        # Display string: "LAN" (uppercase) so the sentinel can't be misread as
        # a person's name ("lan" squints as "Ian").
        "username": username or "LAN",
        "via": "sso" if username else "lan",
    }


def require_bearer(request: Request):
    if not API_BEARER_TOKEN:
        raise HTTPException(401, "bearer auth unconfigured")  # fail closed
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    # Encode both sides: compare_digest raises TypeError on non-ASCII str,
    # which would surface as a 500 instead of a clean 401.
    if scheme.lower() != "bearer" or not hmac.compare_digest(
            presented.strip().encode(), API_BEARER_TOKEN.encode()):
        raise HTTPException(401, "unauthorized")


# Form POSTs ride the domain-wide authentik cookie, so a malicious page could
# submit them cross-site (they're CORS-simple — no preflight to stop them).
# Origin/Referer is the cheap, stateless defense: browsers set it on every
# cross-origin form POST and script can't forge it. Same-origin requests from
# older clients may omit it entirely, which we allow; a PRESENT but foreign
# origin is always rejected.
_ALLOWED_ORIGIN_HOSTS = {"traffic.travismetcalf.com", "traffic.local"}


def require_same_origin(request: Request):
    from urllib.parse import urlparse
    raw = request.headers.get("origin") or request.headers.get("referer")
    if not raw:
        return
    host = (urlparse(raw).hostname or "").lower()
    if host not in _ALLOWED_ORIGIN_HOSTS and host != (request.url.hostname or "").lower():
        raise HTTPException(403, "cross-origin form post rejected")


# ── health ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}   # deliberately no DB touch — compose healthcheck


# ── pages ────────────────────────────────────────────────────────────────────

def _place_view(p: dict) -> dict:
    """A place as a map needs it. `kind` is the stored column, served from HERE
    rather than re-derived in each client, so the house/office pins can't
    diverge between web and iOS — the same reason corridor colors are pinned to
    alt_index. `monitored` becomes a real bool because it crosses into JSON."""
    return {**p, "kind": p.get("kind") or "office",
            "monitored": bool(p.get("monitored", 1))}


def _with_endpoints(route: dict, places: dict) -> dict:
    """Resolve origin_key/dest_key into coordinates. A map can't join on a key,
    and hardcoding the two coordinates client-side breaks the moment someone
    edits a place at /settings."""
    return {**route,
            "origin": places.get(route["origin_key"]),
            "dest": places.get(route["dest_key"])}


def _routes_for_ui(conn):
    # This dict is serialized straight into the page's boot-data, so endpoints
    # resolved here reach the map without a second round trip.
    places = {p["key"]: _place_view(p) for p in get_places(conn)}
    routes = [_with_endpoints(r, places) for r in get_routes(conn)]
    return {
        "outbound": [r for r in routes if r["heading"] == "outbound" and r["role"] != "baseline"],
        "inbound": [r for r in routes if r["heading"] == "inbound" and r["role"] != "baseline"],
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with get_conn() as conn:
        ctx = {
            "user": current_user(request),
            "routes_ui": _routes_for_ui(conn),
            "places": get_places(conn),
            "review_count": conn.execute(
                "SELECT COUNT(*) n FROM trips WHERE needs_review=1 AND verdict='auto'"
            ).fetchone()["n"],
            "headline": stats_mod.headline(conn),
            "cost": stats_mod.cost(conn),
            "bias": stats_mod.bias(conn),
            "plan": stats_mod.plan_week(conn),
            # TRUST zone footer — rendered from Jinja rather than a new API
            # route; the browser only recomputes the AGE of newest_ts_utc.
            "collection": stats_mod.collection_health(conn),
        }
    return templates.TemplateResponse(request, "index.html", ctx)


# Trip rows name their endpoints with the places' own labels. Resolved in SQL
# because place_key is deliberately NOT a foreign key on trips (a deleted or
# renamed place must never rewrite history) — the LEFT JOINs simply leave the
# label NULL and the row falls back to the raw key.
_TRIP_SELECT = """SELECT t.{cols}, r.slug AS route_slug, r.label AS route_label,
                         po.label AS origin_label, pd.label AS dest_label
                  FROM trips t
                  LEFT JOIN routes r ON t.route_id = r.id
                  LEFT JOIN places po ON t.origin_key = po.key
                  LEFT JOIN places pd ON t.dest_key = pd.key"""

# Statuses the trip list shows, web and iOS alike. 'noise' is the only one held
# back: sub-90s pairs are the car moving in the driveway and there are many.
# 'non_commute' MUST be here — it is excluded from averages, and a row excluded
# from the averages but invisible in the list is one whose misclassification
# nobody can ever catch. Listed once so the two surfaces cannot diverge.
_LISTED_STATUSES = "('complete','abandoned','open','non_commute')"


@app.get("/trips", response_class=HTMLResponse)
def trips_page(request: Request):
    with get_conn() as conn:
        # This ORDER BY picks WHICH 200 rows, not the order they are shown in —
        # trips.js re-stacks them client-side (newest first by default). Keep
        # needs_review leading: it is what guarantees a flagged trip stays on
        # the page however old it is. "Fixing" it to match the display would
        # quietly drop old flagged rows out of the window.
        rows = [dict(r) for r in conn.execute(
            _TRIP_SELECT.format(cols="*") +
            f" WHERE t.status IN {_LISTED_STATUSES}"
            " ORDER BY t.needs_review DESC, t.start_utc DESC LIMIT 200")]
        places = get_places(conn)
    return templates.TemplateResponse(request, "trips.html", {
        "user": current_user(request), "trips": rows, "places": places,
    })


@app.post("/trips/{trip_id}/verdict")
def set_verdict(trip_id: int, request: Request, verdict: str = Form(...)):
    require_same_origin(request)
    if verdict not in ("confirmed", "rejected"):
        raise HTTPException(400, "bad verdict")
    with get_conn() as conn:
        conn.execute(
            "UPDATE trips SET verdict=?, needs_review=0, verdict_by=? WHERE id=?",
            # On the LAN surface X-authentik-username is client-controlled, so
            # persist the sentinel rather than a spoofable name.
            (verdict, current_user(request)["username"][:64], trip_id))
        row = conn.execute(_TRIP_SELECT.format(cols="*") + " WHERE t.id=?",
                           (trip_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "partials/trip_row.html", {"t": dict(row)})


@app.post("/trips/manual")
def manual_trip(request: Request, start_local: str = Form(...), end_local: str = Form(...),
                origin_key: str = Form(...), dest_key: str = Form(...)):
    """Log a trip by hand (Phase-1 stopgap; useful forever). Times are ET."""
    require_same_origin(request)
    try:
        s = datetime.fromisoformat(start_local).replace(tzinfo=LOCAL_TZ)
        e = datetime.fromisoformat(end_local).replace(tzinfo=LOCAL_TZ)
    except ValueError:
        raise HTTPException(400, "bad datetime")
    if e <= s:
        raise HTTPException(400, "end before start")
    # place_key lands in the APPEND-ONLY event log, where garbage survives even
    # a --rebuild-all. Whitelist against real places, like /settings/place does.
    with get_conn() as conn:
        known = {p["key"] for p in get_places(conn)}
    if origin_key not in known or dest_key not in known:
        raise HTTPException(400, "unknown place")
    s_epoch, e_epoch = int(s.timestamp()), int(e.timestamp())

    def insert_event(kind, place, epoch):
        key = hashlib.sha256(f"manual|{kind}|{place}|{epoch}".encode()).hexdigest()
        lf = local_fields(datetime.fromtimestamp(epoch, tz=timezone.utc))
        with get_conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO trip_events
                     (event_key, kind, place_key, source, device_ts, server_ts,
                      local_date, local_mod, local_dow)
                   VALUES (?, ?, ?, 'web_manual', ?, ?, ?, ?, ?)""",
                (key, kind, place, epoch, utcnow(),
                 lf["local_date"], lf["local_mod"], lf["local_dow"]))

    insert_event("manual", origin_key, s_epoch)
    insert_event("geofence_enter", dest_key, e_epoch)
    from app.derive import derive
    derive()
    return RedirectResponse("/trips", status_code=303)


def _settings_for_template(conn) -> dict:
    """Every settings key the page renders, resolved.

    The stored table is not enough. The five alert_* keys live in
    brief.ALERT_DEFAULTS and the nine notify_* keys in notify.defaults(); both
    are read-with-fallback by design and are deliberately NOT seeded into
    db.DEFAULT_SETTINGS, so a fresh database has no row for them. Jinja renders
    a missing key as the empty string, so before this helper those inputs came
    up blank and submitting one blank returned 400 bad value.
    """
    resolved = {**brief_mod.ALERT_DEFAULTS, **notify_mod.defaults()}
    resolved.update({r["key"]: r["value"]
                     for r in conn.execute("SELECT key, value FROM settings")})
    return resolved


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, err: str = ""):
    with get_conn() as conn:
        month = datetime.now(LOCAL_TZ).strftime("%Y-%m")
        usage = conn.execute("SELECT calls FROM api_usage WHERE month=?", (month,)).fetchone()
        api_calls_month = usage["calls"] if usage else 0
        cap = get_setting(conn, "monthly_call_cap", "18000")
        places = get_places(conn)
        # The System group's context is the live API-budget readout; every
        # other group's is the static string SETTING_GROUPS already carries.
        # Matched by TITLE, not list position: SETTING_GROUPS is declared in
        # display order and this plan is *about* reordering groups, so
        # `SETTING_GROUPS[3]["context"] = ...` would silently start
        # overwriting a different group's context the day one moves.
        group_context_overrides = {
            "System": f"API budget used this month: {api_calls_month} / {cap}",
        }
        ctx = {
            "user": current_user(request),
            "places": places,
            "has_home": any(p["kind"] == "home" for p in places),
            "routes": get_routes(conn, enabled_only=False),
            "error": _SETTINGS_ERRORS.get(err),
            "groups": [
                {"title": g["title"],
                 "context": group_context_overrides.get(g["title"], g["context"]),
                 "rows": [_row_context(conn, k) for (k, _l, _h) in g["rows"]]}
                for g in SETTING_GROUPS
            ],
            "notifications": [
                {"key": n["key"], "label": n["label"], "help": n["help"],
                 "channels": _switch_columns(n)}
                for n in notify_mod.payload(conn)
            ],
        }
    return templates.TemplateResponse(request, "settings.html", ctx)


# Failure modes a person can hit from a non-htmx form post (JS disabled, or
# the htmx swap itself never arrives). They redirect back to /settings with
# the reason rather than 4xx-ing to a bare JSON page — a form post that
# dead-ends on an error document loses everything the user typed. Named for
# the page, not just Places: /settings/value's validator-rejection path
# shares this same treatment (spec: docs/superpowers/specs/
# 2026-09-17-notifications-and-settings-design.md:267).
_SETTINGS_ERRORS = {
    "in_use": "That place has recorded history, so deleting it would orphan trips "
              "and samples. Untick “monitored” instead — the data stays, the phone "
              "stops watching it, and its corridors become baselines.",
    "unknown": "No such place — it may have been deleted in another tab.",
    "bad_value": "That value didn't look right, so nothing was saved.",
}

_KEY_RE = re.compile(r"[^a-z0-9]+")


def _place_key(existing: set[str], label: str) -> str:
    """A stable, immutable identifier slugified from the label at CREATE time.

    Never recomputed on rename: place_key is written into the append-only
    trip_events log, so changing it would silently detach a place from every
    trip it has ever been an endpoint of."""
    base = _KEY_RE.sub("_", label.lower()).strip("_")[:40] or "place"
    if base not in existing:
        return base
    for n in range(2, 1000):
        if (cand := f"{base}_{n}") not in existing:
            return cand
    raise HTTPException(400, "too many places with that name")


@app.post("/settings/place")
def save_place(request: Request, label: str = Form(...),
               lat: float = Form(...), lon: float = Form(...),
               key: str = Form(""), kind: str = Form("office"),
               monitored: bool = Form(False),
               geofence_radius_m: int = Form(175)):
    """Create (key empty) or update (key present) one place. Anyone's topology:
    the three keys this used to whitelist were one deployment's, and a second
    deployment could not name its own."""
    require_same_origin(request)
    label = label.strip()[:80]
    if not label:
        raise HTTPException(400, "label required")
    if kind not in ("home", "office"):
        raise HTTPException(400, "bad kind")
    if not (0 < geofence_radius_m <= 5000):
        raise HTTPException(400, "bad radius")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "bad coords")

    with get_conn() as conn:
        existing = {p["key"] for p in get_places(conn)}
        if key:
            if key not in existing:
                return RedirectResponse("/settings?err=unknown", status_code=303)
            conn.execute(
                """UPDATE places SET label=?, kind=?, lat=?, lon=?,
                     geofence_radius_m=?, monitored=? WHERE key=?""",
                (label, kind, round(lat, 5), round(lon, 5),
                 geofence_radius_m, int(monitored), key))
        else:
            conn.execute(
                """INSERT INTO places (key, label, kind, lat, lon,
                     geofence_radius_m, monitored)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (_place_key(existing, label), label, kind, round(lat, 5),
                 round(lon, 5), geofence_radius_m, int(monitored)))
        _sync_routes(conn)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/place/delete")
def delete_place(request: Request, key: str = Form(...)):
    """Drop a place that was added by mistake. Refused once anything references
    it: trips and trip_events keep place keys as plain text with no FK, so a
    delete would leave rows pointing at a place that no longer exists, and
    dropping its routes would take every predicted sample with them."""
    require_same_origin(request)
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM places WHERE key=?", (key,)).fetchone():
            return RedirectResponse("/settings?err=unknown", status_code=303)
        used = conn.execute(
            """SELECT (SELECT COUNT(*) FROM trip_events WHERE place_key=?)
                    + (SELECT COUNT(*) FROM trips WHERE origin_key=? OR dest_key=?)
                    + (SELECT COUNT(*) FROM predicted_samples ps
                       JOIN routes r ON ps.route_id=r.id
                       WHERE r.origin_key=? OR r.dest_key=?) AS n""",
            (key, key, key, key, key)).fetchone()["n"]
        if used:
            return RedirectResponse("/settings?err=in_use", status_code=303)
        conn.execute(
            """DELETE FROM route_geometry WHERE route_id IN
                 (SELECT id FROM routes WHERE origin_key=? OR dest_key=?)""", (key, key))
        conn.execute("DELETE FROM routes WHERE origin_key=? OR dest_key=?", (key, key))
        conn.execute("DELETE FROM places WHERE key=?", (key,))
        _sync_routes(conn)
    return RedirectResponse("/settings", status_code=303)


def _sync_routes(conn):
    """Derive the route set from whatever places exist: every home↔office pair,
    both directions. Idempotent, and safe to run on every place edit.

    A pair that already has a route is UPDATED, never re-inserted — route_id is
    what years of predicted_samples hang off, so the row has to survive renames
    and role flips. Matching is on (origin_key, dest_key) rather than slug for
    the same reason: deployments that predate user-defined places still carry
    the old hand-written slugs, and a slug-keyed seed would insert duplicate
    corridors beside them.

    role/cadence follow `monitored`: a place you still drive to gets full-rate
    polling with alternates, one you have retired keeps collecting as a
    baseline at a third of the cost.
    """
    places = get_places(conn)
    homes = [p for p in places if p["kind"] == "home"]
    offices = [p for p in places if p["kind"] != "home"]
    for home in homes:
        for office in offices:
            primary = bool(office["monitored"])
            role = "primary" if primary else "baseline"
            divisor, alts = (1, 2) if primary else (3, 0)
            for origin, dest, heading in ((home, office, "outbound"),
                                          (office, home, "inbound")):
                _upsert_route(conn, origin, dest, heading, role, divisor, alts)


def _upsert_route(conn, origin: dict, dest: dict, heading: str, role: str,
                  divisor: int, alts: int):
    label = f"{origin['label']} → {dest['label']}"
    row = conn.execute(
        """SELECT id FROM routes WHERE origin_key=? AND dest_key=?
             AND role != 'alternate' ORDER BY id LIMIT 1""",
        (origin["key"], dest["key"])).fetchone()
    if row:
        conn.execute(
            """UPDATE routes SET label=?, heading=?, role=?, poll_divisor=?,
                 max_alternatives=? WHERE id=?""",
            (label, heading, role, divisor, alts, row["id"]))
        return
    conn.execute(
        """INSERT INTO routes (slug, label, origin_key, dest_key, heading, role,
             poll_divisor, max_alternatives)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (_route_slug(conn, origin["key"], dest["key"]), label, origin["key"],
         dest["key"], heading, role, divisor, alts))


def _route_slug(conn, origin_key: str, dest_key: str) -> str:
    """Human-readable but machine-stable: it is the id in /api/stats?route= and
    in every client's URL, so it is generated once and never regenerated."""
    taken = {r["slug"] for r in conn.execute("SELECT slug FROM routes")}
    base = f"{origin_key}-to-{dest_key}".replace("_", "-")[:80]
    if base not in taken:
        return base
    for n in range(2, 1000):
        if (cand := f"{base}-{n}") not in taken:
            return cand
    raise HTTPException(400, "too many routes for that pair")


_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _int_in(lo: int, hi: int):
    def check(v: str) -> bool:
        try:
            return lo <= int(v) <= hi
        except ValueError:
            return False
    return check


def _float_in(lo: float, hi: float):
    def check(v: str) -> bool:
        try:
            return lo <= float(v) <= hi
        except ValueError:
            return False
    return check


def _dow_csv(v: str) -> bool:
    parts = [x.strip() for x in v.split(",") if x.strip() != ""]
    return bool(parts) and all(p.isdigit() and 0 <= int(p) <= 6 for p in parts)


# Every settable value gets a TYPE, not just a whitelisted key. Values reach
# shell arithmetic (scripts/smoke.sh) and float()/int() casts that render the
# dashboard — an unvalidated string here was a remote-code-execution primitive
# and a one-request way to 500 every page.
_SETTING_VALIDATORS = {
    "monthly_call_cap": _int_in(1, 1_000_000),
    "anomaly_min_delta_min": _int_in(0, 600),
    "anomaly_min_live_days": _int_in(0, 3650),
    "mpg": _float_in(1, 200),
    "fuel_price_per_gal": _float_in(0, 100),
    "office_days_per_week": _float_in(0, 7),
    "office_hours_per_day": _float_in(0, 24),
    "earliest_leave_home": lambda v: bool(_HHMM_RE.match(v)),
    "latest_leave_office": lambda v: bool(_HHMM_RE.match(v)),
    "alert_days": _dow_csv,
    "alert_am_start": lambda v: bool(_HHMM_RE.match(v)),
    "alert_am_end": lambda v: bool(_HHMM_RE.match(v)),
    "alert_pm_start": lambda v: bool(_HHMM_RE.match(v)),
    "alert_pm_end": lambda v: bool(_HHMM_RE.match(v)),
    # Notification switches come from the catalog rather than being listed
    # here: one entry in notify.CATALOG must be enough to add a type, or the
    # two UIs and this dict drift apart silently.
    **notify_mod.validators(),
}


#: The settings page's information architecture. One list, so a key cannot be
#: rendered twice or lost — the fourteen-row Tunables card had no such guard.
#: Notifications are NOT here: they come from notify.payload(), which knows its
#: own keys.
SETTING_GROUPS = [
    {"title": "Your commute",
     "context": "the facts the cost and planning math needs",
     "rows": [
         ("office_days_per_week", "Office days per week",
          "Hybrid schedule — drives the week and year cost, and how many days "
          "Plan-your-week picks"),
         ("office_hours_per_day", "Hours at the office",
          "Used to chain morning departure to evening traffic"),
         ("mpg", "Vehicle MPG", "For the fuel cost line"),
         ("fuel_price_per_gal", "Fuel price per gallon", ""),
     ]},
    {"title": "Planning",
     "context": "bounds on what Plan-your-week will suggest",
     "rows": [
         ("earliest_leave_home", "Earliest leave home",
          "Won't recommend departures before this"),
         ("latest_leave_office", "Latest leave office",
          "Won't recommend returns after this"),
     ]},
    {"title": "Commute alerts",
     "context": "when the unusually-bad-traffic check may fire",
     "rows": [
         ("alert_days", "Alert days", "0=Mon … 6=Sun, comma-separated"),
         ("alert_am_start", "Morning window start", ""),
         ("alert_am_end", "Morning window end", ""),
         ("alert_pm_start", "Evening window start", ""),
         ("alert_pm_end", "Evening window end", ""),
         ("anomaly_min_delta_min", "Minimum delta",
          "Alert only when above p90 AND median plus this"),
         ("anomaly_min_live_days", "Arming period",
          "Alerts stay off until this many days of live data exist"),
     ]},
    {"title": "System",
     "context": "",
     "rows": [
         ("monthly_call_cap", "Monthly API call cap",
          "Hard stop — the poller degrades to no-new-data rather than risk the key"),
     ]},
]

#: Flattened once so a lookup is O(1) and cannot disagree with what the page
#: renders — both read this.
SETTING_ROW_BY_KEY = {k: (label, help_)
                      for g in SETTING_GROUPS for (k, label, help_) in g["rows"]}


def _row_context(conn, key: str, saved: bool = False) -> dict:
    """The context for one settings row, for both the page and the htmx swap.

    Notification keys are not in SETTING_GROUPS — they come from the catalog,
    which owns its own keys. Look there second so a notify_* save re-renders
    the whole switch pair, not just the one that changed: both switches of a
    type live in one row, and swapping half of it would leave the other stale.
    """
    resolved = _settings_for_template(conn)
    if key in SETTING_ROW_BY_KEY:
        label, help_ = SETTING_ROW_BY_KEY[key]
        return {"key": key, "label": label, "help": help_,
                "kind": "field", "value": resolved.get(key, ""), "saved": saved}
    for n in notify_mod.payload(conn):
        if any(s["setting_key"] == key for s in n["switches"]):
            return {"key": n["key"], "label": n["label"], "help": n["help"],
                    "kind": "switch", "channels": _switch_columns(n),
                    "saved": saved}
    raise KeyError(f"no settings row renders {key}")


def _switch_columns(entry: dict) -> list[dict]:
    """Email column then push column, with a placeholder for a channel this
    type cannot use — built HERE, not in Jinja. Dict surgery in a template is
    how the web and iOS renderings drift apart.
    """
    by_channel = {s["channel"]: s for s in entry["switches"]}
    out = []
    for want in ("email", "push"):
        s = by_channel.get(want)
        out.append({"setting_key": s["setting_key"], "channel": want,
                    "on": s["enabled"]} if s else {"setting_key": None})
    return out


@app.post("/settings/value")
def save_setting(request: Request, key: str = Form(...), value: str = Form(...)):
    require_same_origin(request)
    validator = _SETTING_VALIDATORS.get(key)
    if validator is None:
        raise HTTPException(400, "bad key")
    value = value.strip()
    if len(value) > 64 or not validator(value):
        # A rejected save must not dead-end: htmx only swaps on a 2xx, so a
        # bare HTTPException(400) here used to leave the bad text sitting in
        # the field with zero signal — the user believed it saved. Re-render
        # from STORED truth (the bad value was never written) at 200, with
        # the rejection visible via the row partial's `error` span, the same
        # redirect-with-reason treatment the Places form uses on the non-htmx
        # path. See docs/superpowers/specs/
        # 2026-09-17-notifications-and-settings-design.md:267.
        if request.headers.get("HX-Request"):
            with get_conn() as conn:
                ctx = _row_context(conn, key, saved=False)
            # Short: it sits inline next to where `saved` would go, not in a
            # banner — the full explanation is the redirect's job, below.
            ctx["row_error"] = "not saved — invalid value"
            return templates.TemplateResponse(
                request, "partials/setting_row.html", ctx)
        return RedirectResponse("/settings?err=bad_value", status_code=303)
    with get_conn() as conn:
        set_setting(conn, key, value)
        if request.headers.get("HX-Request"):
            # Re-render from what was actually stored, not from what was
            # submitted — a save that silently normalised the value would
            # otherwise leave the control showing the wrong state.
            return templates.TemplateResponse(
                request, "partials/setting_row.html",
                _row_context(conn, key, saved=True))
    return RedirectResponse("/settings", status_code=303)


# ── stats API (SSO surface) ──────────────────────────────────────────────────

def _since(days: int) -> str:
    from datetime import timedelta
    return (datetime.now(LOCAL_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")


def _route_id(conn, slug: str) -> int:
    row = conn.execute("SELECT id FROM routes WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown route")
    return row["id"]


@app.get("/api/stats/hourly")
def api_hourly(route: str, days: int = 90):
    with get_conn() as conn:
        rid = _route_id(conn, route)
        heading = conn.execute("SELECT heading FROM routes WHERE id=?", (rid,)).fetchone()["heading"]
        return {"buckets": stats_mod.hourly(conn, rid, _since(days)),
                "actual": stats_mod.actual_hourly(conn, rid, heading)}


@app.get("/api/stats/dow")
def api_dow(route: str, days: int = 90):
    with get_conn() as conn:
        return {"buckets": stats_mod.dow(conn, _route_id(conn, route), _since(days))}


@app.get("/api/stats/heatmap")
def api_heatmap(route: str, days: int = 90):
    with get_conn() as conn:
        return {"cells": stats_mod.heatmap(conn, _route_id(conn, route), _since(days))}


@app.get("/api/stats/headline")
def api_headline(days: int = 90):
    with get_conn() as conn:
        return stats_mod.headline(conn, _since(days))


@app.get("/api/stats/cost")
def api_cost(days: int = 90):
    with get_conn() as conn:
        return stats_mod.cost(conn, _since(days))


@app.get("/api/stats/bias")
def api_bias():
    with get_conn() as conn:
        return stats_mod.bias(conn)


def _route_payload(conn, rid, alt_index, allow_live=True):
    row = conn.execute(
        """SELECT ps.*, rg.points_json, rg.bbox_json FROM predicted_samples ps
           LEFT JOIN route_geometry rg ON ps.geometry_id = rg.id
           WHERE ps.route_id=? AND ps.kind='live' AND ps.alt_index=?
           ORDER BY ps.ts_utc DESC LIMIT 1""", (rid, alt_index)).fetchone()

    stale = True
    if row:
        age = (datetime.now(timezone.utc)
               - datetime.strptime(row["ts_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
        stale = age.total_seconds() > STALE_AFTER_SEC

    if stale and allow_live and alt_index == 0 and TOMTOM_API_KEY:
        fresh = _live_route_fetch(conn, rid)
        if fresh:
            return fresh

    if not row or not row["points_json"]:
        return None
    payload = _geojson_payload(json.loads(row["points_json"]),
                               json.loads(row["sections_json"] or "[]"),
                               json.loads(row["bbox_json"]),
                               {k: row[k] for k in ("travel_sec", "free_flow_sec",
                                                    "historic_sec", "delay_sec", "ts_utc")},
                               stale)
    payload["alt_index"] = alt_index
    payload["length_meters"] = row["length_meters"]
    return payload


def _live_alts(conn, rid) -> list[dict]:
    """Newest live sample per corridor, primary first — the NOW card's
    comparison set, without the geometry /api/route hauls along.

    One query, not one per corridor: SQLite's documented bare-column rule makes
    every unaggregated column come from the row that won MAX(ts_utc).

    A corridor with no live sample is simply absent, so callers never have to
    render a row for a time that doesn't exist."""
    now = datetime.now(timezone.utc)
    out = []
    for r in conn.execute(
            """SELECT alt_index, travel_sec, free_flow_sec, historic_sec,
                      delay_sec, length_meters, MAX(ts_utc) AS ts_utc
               FROM predicted_samples
               WHERE route_id=? AND kind='live' AND alt_index < ?
               GROUP BY alt_index ORDER BY alt_index""", (rid, ALT_SLOTS)):
        d = dict(r)
        age = now - datetime.strptime(d["ts_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        d["stale"] = age.total_seconds() > STALE_AFTER_SEC
        out.append(d)
    return out


@app.get("/api/route")
def api_route(route: str, alts: int = 0):
    """Newest sample + geometry as GeoJSON, split per congestion section so the
    browser does zero index arithmetic. Served from the DB; a live TomTom call
    happens only out-of-window, TTL+daily capped (primary only — alternates are
    at most one poll cycle old inside the window).

    alts=1 returns ALL alternates: {"alts": [payload, ...], "bbox": union} —
    payload[0] is the primary with congestion sections; the union bbox frames
    every corridor at once."""
    with get_conn() as conn:
        rid = _route_id(conn, route)
        if not alts:
            p = _route_payload(conn, rid, 0)
            return p or {"features": [], "summary": None, "stale": True}
        payloads = [p for i in range(ALT_SLOTS)
                    if (p := _route_payload(conn, rid, i, allow_live=(i == 0)))]
        bbox = None
        for p in payloads:
            b = p.get("bbox")
            if not b:
                continue
            bbox = b if bbox is None else [min(bbox[0], b[0]), min(bbox[1], b[1]),
                                           max(bbox[2], b[2]), max(bbox[3], b[3])]
        return {"alts": payloads, "bbox": bbox}


def _geojson_payload(points, sections, bbox, summary, stale):
    feats = []
    covered = [False] * len(points)
    for sec in sections:
        s, e = sec.get("s") or 0, sec.get("e") or 0
        if e > s:
            for i in range(s, min(e + 1, len(points))):
                covered[i] = True
            feats.append({"type": "Feature", "properties": {"mag": sec.get("mag", 0)},
                          "geometry": {"type": "LineString", "coordinates": points[s:e + 1]}})
    # uncovered stretches → neutral features
    i = 0
    while i < len(points):
        if covered[i]:
            i += 1
            continue
        j = i
        while j < len(points) and not covered[j]:
            j += 1
        if j - i >= 2:
            feats.append({"type": "Feature", "properties": {"mag": None},
                          "geometry": {"type": "LineString", "coordinates": points[i:j]}})
        i = j
    return {"type": "FeatureCollection", "features": feats,
            "bbox": bbox, "summary": summary, "stale": stale}


def _live_route_fetch(conn, rid):
    from app import tomtom
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    if _live_calls["date"] != today:
        _live_calls.update(date=today, n=0)
    now = time.time()
    cached = _route_cache.get(rid)
    if cached and now - cached[0] < LIVE_TTL_SEC:
        return cached[1]
    if _live_calls["n"] >= LIVE_MAX_PER_DAY:
        return None
    cap = int(get_setting(conn, "monthly_call_cap", "18000"))
    month = datetime.now(LOCAL_TZ).strftime("%Y-%m")
    usage = conn.execute("SELECT calls FROM api_usage WHERE month=?", (month,)).fetchone()
    if usage and usage["calls"] >= cap:
        return None

    r = conn.execute("SELECT * FROM routes WHERE id=?", (rid,)).fetchone()
    places = {p["key"]: p for p in get_places(conn)}
    o, d = places.get(r["origin_key"]), places.get(r["dest_key"])
    if not o or not d:
        return None
    try:
        body = tomtom.calculate_route(TOMTOM_API_KEY, (o["lat"], o["lon"]), (d["lat"], d["lon"]),
                                      via=json.loads(r["via_json"]) if r["via_json"] else None)
    except Exception:
        return None
    _live_calls["n"] += 1
    route0 = (body.get("routes") or [None])[0]
    if not route0:
        return None
    pts = tomtom.extract_points(route0)
    s = tomtom.extract_summary(route0)
    s["ts_utc"] = utcnow()
    payload = _geojson_payload(pts, tomtom.extract_sections(route0), tomtom.bbox(pts), s, False)
    # Same top-level keys as the DB path: clients index corridor color and
    # label off alt_index, and this payload substitutes for the primary.
    payload["alt_index"] = 0
    payload["length_meters"] = s.get("length_meters")
    # NOT persisted — out-of-window samples must never pollute the statistics.
    _route_cache[rid] = (now, payload)
    return payload


# ── tile proxy (SSO surface) ─────────────────────────────────────────────────

_TILE_RE = re.compile(r"^\d{1,2}$")


@app.get("/tiles/{layer}/{z}/{x}/{y}.png")
def tiles(layer: str, z: str, x: str, y: str):
    if layer not in ("base", "flow") or not TOMTOM_API_KEY:
        raise HTTPException(404)
    if not (_TILE_RE.match(z) and x.isdigit() and y.isdigit()):
        raise HTTPException(400)

    if layer == "base":
        cache_path = os.path.join(TILE_CACHE, f"base-{z}-{x}-{y}.png")
        if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < 30 * 86400:
            return Response(open(cache_path, "rb").read(), media_type="image/png",
                            headers={"Cache-Control": "max-age=86400"})
        url = f"https://api.tomtom.com/map/1/tile/basic/main/{z}/{x}/{y}.png"
    else:
        url = f"https://api.tomtom.com/traffic/map/4/tile/flow/relative0/{z}/{x}/{y}.png"

    try:
        resp = httpx.get(url, params={"key": TOMTOM_API_KEY}, timeout=10)
    except Exception:
        raise HTTPException(502, "tile upstream failed")
    if resp.status_code != 200:
        raise HTTPException(resp.status_code)

    if layer == "base":
        tmp = cache_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(resp.content)
        os.replace(tmp, cache_path)
        return Response(resp.content, media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})
    return Response(resp.content, media_type="image/png",
                    headers={"Cache-Control": "max-age=60"})


# ── /api/v1 (bearer surface — iOS app + widget) ──────────────────────────────

def _should_derive(results: list[dict], events: list[dict]) -> bool:
    """True when this batch actually appended a trip-ending event.

    Keyed on 'ok' rather than presence: the phone replays a durable outbox, so
    most batches are duplicates of events already derived. Deriving on those
    would rebuild the whole log on every flush for no new information.
    """
    new = {r["event_key"] for r in results if r.get("status") == "ok"}
    return any(e.get("event_key") in new and e.get("kind") in END_KINDS
               for e in events)


@app.post("/api/v1/ingest/events")
async def ingest_events(request: Request):
    """Batch event ingest. Built for a client that replays an outbox: idempotent
    on event_key, and a duplicate returns ok (a non-2xx would make the client
    retry forever / surface an error for a benign case)."""
    require_bearer(request)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"status": "bad_json"}, status_code=400)
    events = payload.get("events")
    if not isinstance(events, list) or len(events) > 500:
        return JSONResponse({"status": "bad_shape"}, status_code=400)

    results = []
    with get_conn() as conn:
        for ev in events:
            ok, why = _validate_event(ev)
            if not ok:
                results.append({"event_key": ev.get("event_key"), "status": "rejected",
                                "reason": why})
                continue
            lf = local_fields(datetime.fromtimestamp(ev["device_ts"], tz=timezone.utc))
            cur = conn.execute(
                """INSERT OR IGNORE INTO trip_events
                     (event_key, kind, place_key, source, confidence, device_ts,
                      server_ts, local_date, local_mod, local_dow, lat, lon, acc, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ev["event_key"], ev["kind"], ev.get("place_key"), ev["source"],
                 ev.get("confidence"), ev["device_ts"], utcnow(),
                 lf["local_date"], lf["local_mod"], lf["local_dow"],
                 ev.get("lat"), ev.get("lon"), ev.get("acc"),
                 json.dumps(ev, separators=(",", ":"))))
            results.append({"event_key": ev["event_key"],
                            "status": "ok" if cur.rowcount else "duplicate"})

    # The transaction is committed at this point — the `with get_conn()` block
    # above has exited. Everything below runs OUTSIDE it, deliberately:
    # derive() opens its own write transaction, and push._send() writes to
    # devices on an APNs 410. A nested write against a held write lock blocks
    # for the full busy_timeout and then raises 'database is locked'.
    #
    # It also runs OFF the event loop, via run_in_threadpool: this handler is
    # `async def`, so a synchronous derive() (a full event-log replay) and
    # notify_completed_trips() (up to 10s of APNs round trip per device) would
    # otherwise block every other request on this process. Measured against
    # the real app: /health at 0.054s baseline, 1.505s when issued mid-ingest
    # with derive stubbed to a 2s sleep.
    if _should_derive(results, events):
        def _post_commit():
            try:
                from app.derive import derive
                derive()
                from app.trip_notify import notify_completed_trips
                notify_completed_trips()
            except Exception as exc:                # noqa: BLE001
                # Ingest has already succeeded and the events are durably
                # stored. A derive or notify fault must never turn that into
                # a non-2xx, which would make the phone retry a batch it
                # already delivered.
                print(f"ingest post-commit: {type(exc).__name__}: {exc}", flush=True)
        await run_in_threadpool(_post_commit)
    return {"results": results}


def _validate_event(ev) -> tuple[bool, str | None]:
    if not isinstance(ev, dict):
        return False, "not an object"
    if not isinstance(ev.get("event_key"), str) or not (16 <= len(ev["event_key"]) <= 128):
        return False, "bad event_key"
    if ev.get("kind") not in VALID_EVENT_KINDS:
        return False, "bad kind"
    if ev.get("source") not in VALID_SOURCES:
        return False, "bad source"
    ts = ev.get("device_ts")
    if not isinstance(ts, (int, float)) or not (1_500_000_000 < ts < 4_000_000_000):
        return False, "bad device_ts"
    return True, None


@app.post("/api/v1/ingest/track")
async def ingest_track(request: Request):
    """Route polyline for a completed trip, keyed by the trip's start event."""
    require_bearer(request)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"status": "bad_json"}, status_code=400)
    start_key = payload.get("start_event_key")
    points = payload.get("points")
    if not isinstance(start_key, str) or not isinstance(points, list) or len(points) < 2:
        return JSONResponse({"status": "bad_shape"}, status_code=400)
    if len(points) > 20000:
        return JSONResponse({"status": "too_many_points"}, status_code=400)

    with get_conn() as conn:
        trip = conn.execute("SELECT id FROM trips WHERE start_event_key=?", (start_key,)).fetchone()
        if not trip:
            # trip not derived yet — accept-and-park is more complex; the iOS
            # client simply retries after the next derive cycle.
            return JSONResponse({"status": "trip_not_found"}, status_code=409)
        lons = [p[0] for p in points]
        lats = [p[1] for p in points]
        conn.execute(
            """INSERT OR REPLACE INTO trip_tracks
                 (trip_id, points_json, bbox_json, length_meters, point_count)
               VALUES (?, ?, ?, NULL, ?)""",
            (trip["id"], json.dumps(points, separators=(",", ":")),
             json.dumps([min(lons), min(lats), max(lons), max(lats)]), len(points)))
    return {"status": "ok"}


@app.post("/api/v1/devices")
async def register_device(request: Request):
    require_bearer(request)
    try:
        p = await request.json()
    except Exception:
        return JSONResponse({"status": "bad_json"}, status_code=400)
    token, env = p.get("device_token"), p.get("apns_env")
    if not isinstance(token, str) or env not in ("sandbox", "production"):
        return JSONResponse({"status": "bad_shape"}, status_code=400)
    now = utcnow()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO devices (device_token, bundle_id, apns_env, model,
                 os_version, app_version, registered_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(device_token) DO UPDATE SET apns_env=excluded.apns_env,
                 os_version=excluded.os_version, app_version=excluded.app_version,
                 last_seen_at=excluded.last_seen_at, enabled=1""",
            (token, p.get("bundle_id", ""), env, p.get("model"),
             p.get("os_version"), p.get("app_version"), now, now))
    return {"status": "ok"}


def _route_ref(route: dict | None, places: dict) -> dict | None:
    """The identity a client needs to name a corridor: the slug every
    /api/*?route= takes, plus the labels to print. Serving this is what lets a
    client stop knowing which slug means "the morning commute" — the one piece
    of knowledge that used to be hardcoded on both surfaces."""
    if not route:
        return None
    origin, dest = places.get(route["origin_key"]), places.get(route["dest_key"])
    return {
        "slug": route["slug"], "label": route["label"],
        "origin_label": origin["label"] if origin else route["origin_key"],
        "dest_label": dest["label"] if dest else route["dest_key"],
    }


@app.get("/api/v1/summary")
def api_summary(request: Request):
    """Compact payload for the widget + iOS native views."""
    require_bearer(request)
    now = datetime.now(LOCAL_TZ)
    direction = "outbound" if now.hour < 12 else "inbound"
    with get_conn() as conn:
        primary = {h: find_route(conn, h) for h in ("outbound", "inbound")}
        route = primary[direction]
        newest = None
        alts = []
        if route:
            newest = conn.execute(
                """SELECT travel_sec, historic_sec, delay_sec, ts_utc
                   FROM predicted_samples
                   WHERE route_id=? AND kind='live' AND alt_index=0
                   ORDER BY ts_utc DESC LIMIT 1""", (route["id"],)).fetchone()
            alts = _live_alts(conn, route["id"])
        places = {p["key"]: p for p in get_places(conn)}
        hl = stats_mod.headline(conn)
    return {
        "direction": direction,
        # Same shape and key as /api/v1/dashboard, deliberately: the widget and
        # the complication decode a different payload from the app, and one
        # naming convention across both is what stops the three from drifting.
        # Additive — builds already on devices ignore it.
        "primary": {h: _route_ref(r, places) for h, r in primary.items()},
        # "now" stays exactly as shipped — the widget and the watch complication
        # decode it in builds that are already on devices. "alts" is additive;
        # alts[0] is the same corridor as "now", carried again so a client can
        # render one uniform list instead of special-casing the primary.
        "now": dict(newest) if newest else None,
        "alts": alts,
        "am_out": hl["am_out"], "pm_in": hl["pm_in"],
        "am_multiplier": hl["am_multiplier"], "pm_multiplier": hl["pm_multiplier"],
        "generated_at": utcnow(),
    }


@app.get("/api/v1/config")
def api_config(request: Request):
    """Places, radii, thresholds — so the iOS app isn't hardcoded. Routes carry
    their endpoints pre-resolved; the map needs coordinates, not place keys."""
    require_bearer(request)
    with get_conn() as conn:
        places = {p["key"]: _place_view(p) for p in get_places(conn)}
        return {
            "places": list(places.values()),
            "routes": [_with_endpoints(
                           {k: r[k] for k in ("slug", "label", "origin_key",
                                              "dest_key", "heading", "role")}, places)
                       for r in get_routes(conn)],
            "max_trip_hours": 4,
            "noise_under_sec": 90,
            "alerts": {k: get_setting(conn, k, v)
                       for k, v in brief_mod.ALERT_DEFAULTS.items()},
            "notifications": notify_mod.payload(conn),
        }


def _norm_setting_value(v) -> str:
    """A Swift client encodes a switch as a JSON boolean; str(True) is 'True',
    which no validator accepts. Normalise before validating, not after."""
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def _api_setting_validators() -> dict:
    """What the bearer surface may write: the alert windows plus every
    notification switch. Same TYPE-checked discipline as the web form — this
    path reaches the same settings table, so it needs the same floor.

    The alert-window keys are DERIVED from brief_mod.ALERT_DEFAULTS, reusing
    _SETTING_VALIDATORS rather than re-listing them here — that literal once
    drifted from ALERT_DEFAULTS (the read side /api/v1/config derives from),
    which meant adding a sixth alert key would advertise it in `alerts` while
    silently dropping writes to it: filtered out of `updates`, no error, the
    client just gets back an empty `updated` list."""
    allowed = {k: _SETTING_VALIDATORS[k] for k in brief_mod.ALERT_DEFAULTS}
    allowed.update(notify_mod.validators())
    return allowed


def _apply_setting_updates(p: dict, allowed: dict) -> tuple[dict, list]:
    """Split a posted body into the updates we will store and the keys we
    reject. Pure, so the 64-char bound and the type checks are testable
    without an HTTP client — this endpoint writes to the settings table
    that feeds smoke.sh's shell arithmetic."""
    updates = {k: _norm_setting_value(v) for k, v in p.items() if k in allowed}
    bad = [k for k, v in updates.items() if len(v) > 64 or not allowed[k](v)]
    return updates, bad


@app.post("/api/v1/settings")
async def api_v1_settings(request: Request):
    """Alert-window settings and notification switches from the iOS app.
    Same whitelist idea as the web form, extended to cover both key
    families; values validated hard."""
    require_bearer(request)
    try:
        p = await request.json()
    except Exception:
        return JSONResponse({"status": "bad_json"}, status_code=400)

    allowed = _api_setting_validators()
    updates, bad = _apply_setting_updates(p, allowed)
    if bad:
        return JSONResponse({"status": "bad_value", "keys": bad}, status_code=400)
    with get_conn() as conn:
        for k, v in updates.items():
            set_setting(conn, k, v)
    return {"status": "ok", "updated": sorted(updates)}


@app.get("/api/v1/dashboard")
def api_dashboard(request: Request):
    """Consolidated payload for the native iOS dashboard — one request renders
    the whole tab. Mirrors the web IA: headline+dots, plan, per-direction
    hourly/dow/heatmap, cost, bias."""
    require_bearer(request)
    with get_conn() as conn:
        places = {p["key"]: p for p in get_places(conn)}
        # The two corridors this tab is about, resolved by (role, heading).
        # Every map below stays keyed by SLUG — that contract is unchanged —
        # but the client now learns which slug is which from "primary" instead
        # of assuming the seed names of one particular deployment.
        primary = {h: find_route(conn, h) for h in ("outbound", "inbound")}
        per_route, newest, alts = {}, {}, {}
        for route in filter(None, primary.values()):
            slug, rid = route["slug"], route["id"]
            per_route[slug] = {
                "hourly": stats_mod.hourly(conn, rid),
                "dow": stats_mod.dow(conn, rid),
                "heatmap": stats_mod.heatmap(conn, rid),
                "actual_hourly": stats_mod.actual_hourly(conn, rid, route["heading"]),
            }
            row = conn.execute(
                """SELECT travel_sec, free_flow_sec, historic_sec, delay_sec, ts_utc
                   FROM predicted_samples
                   WHERE route_id=? AND kind='live' AND alt_index=0
                   ORDER BY ts_utc DESC LIMIT 1""", (rid,)).fetchone()
            newest[slug] = dict(row) if row else None
            alts[slug] = _live_alts(conn, rid)
        return {
            "primary": {h: _route_ref(r, places) for h, r in primary.items()},
            "headline": stats_mod.headline(conn),
            "plan": stats_mod.plan_week(conn),
            "reliability": stats_mod.route_reliability(conn),
            "cost": stats_mod.cost(conn),
            "bias": stats_mod.bias(conn),
            "now": newest,
            # Keyed like "now" so the NOW card reads both from the same slug —
            # alts[slug][0] is the same corridor as now[slug].
            "alts": alts,
            "routes": per_route,
            "generated_at": utcnow(),
        }


@app.get("/api/stats/reliability")
def api_reliability():
    with get_conn() as conn:
        return stats_mod.route_reliability(conn)


@app.get("/api/v1/route")
def api_v1_route(request: Request, route: str, alts: int = 0):
    """Route GeoJSON on the bearer surface (native MapKit polyline)."""
    require_bearer(request)
    return api_route(route, alts)


@app.get("/api/v1/trips")
def api_v1_trips(request: Request, limit: int = 50):
    require_bearer(request)
    cols = ("id, t.local_date, t.local_mod, t.start_utc, t.end_utc, "
            "t.duration_sec, t.predicted_sec, t.status, t.verdict, "
            "t.needs_review, t.review_reason, t.origin_key, t.dest_key")
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            _TRIP_SELECT.format(cols=cols) +
            f" WHERE t.status IN {_LISTED_STATUSES}"
            " ORDER BY t.needs_review DESC, t.start_utc DESC LIMIT ?",
            (min(limit, 200),))]
    return {"trips": rows}


@app.post("/api/v1/trips/{trip_id}/verdict")
async def api_v1_verdict(trip_id: int, request: Request):
    require_bearer(request)
    try:
        p = await request.json()
    except Exception:
        return JSONResponse({"status": "bad_json"}, status_code=400)
    verdict = p.get("verdict")
    if verdict not in ("confirmed", "rejected"):
        return JSONResponse({"status": "bad_verdict"}, status_code=400)
    with get_conn() as conn:
        conn.execute(
            "UPDATE trips SET verdict=?, needs_review=0, verdict_by='ios' WHERE id=?",
            (verdict, trip_id))
    return {"status": "ok"}

