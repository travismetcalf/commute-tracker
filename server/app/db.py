"""SQLite layer, house pattern: DB_PATH from env, one
executescript in init_db(), context-managed connections with WAL.

Two writers share this DB (host poller on native APFS, container via VirtioFS),
so busy_timeout is explicit and every job holds one short transaction.

All local-time fields (local_date/local_hour/local_mod/local_dow) are computed
HERE, in Python, with zoneinfo at write time — never with SQLite's `localtime`,
which depends on the ambient TZ and mishandles DST. See local_fields().
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DB_PATH = os.environ.get("DB_PATH", "/app/data/commute.db")
LOCAL_TZ = ZoneInfo("America/New_York")

SCHEMA = """
    -- Named endpoints, entirely user-defined at /settings. Coordinates entered
    -- by hand (never committed). `key` is slugified from the label at creation
    -- and then IMMUTABLE: trip_events is append-only and stores place_key, so a
    -- rename that changed the key would orphan every trip ever recorded.
    CREATE TABLE IF NOT EXISTS places (
        key                TEXT PRIMARY KEY,
        label              TEXT NOT NULL,
        kind               TEXT NOT NULL DEFAULT 'office',  -- 'home' | 'office'
        lat                REAL NOT NULL,
        lon                REAL NOT NULL,
        geofence_radius_m  INTEGER NOT NULL DEFAULT 175,  -- consumed by the iOS app
        -- "I still drive here": the phone geofences it and its corridors poll at
        -- full rate. Unmonitored places keep their coordinates and their history
        -- as a baseline to compare against — they are never hidden from
        -- /api/v1/config, or the baseline corridors lose their map endpoints.
        monitored          INTEGER NOT NULL DEFAULT 1
    );

    -- Every tracked route. Generalises "direction" so baseline and alternate
    -- corridors are first-class rather than bolted on. Rows are derived from
    -- `places` by _sync_routes() — one pair per home/office combination.
    CREATE TABLE IF NOT EXISTS routes (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        slug             TEXT NOT NULL UNIQUE,  -- '<origin>-to-<dest>', stable id
        label            TEXT NOT NULL,
        origin_key       TEXT NOT NULL REFERENCES places(key),
        dest_key         TEXT NOT NULL REFERENCES places(key),
        heading          TEXT NOT NULL,         -- 'outbound' (to office) | 'inbound' (to home)
        via_json         TEXT,                  -- [[lat,lon],...] waypoints forcing a corridor
        role             TEXT NOT NULL,         -- 'primary' | 'baseline' | 'alternate'
        poll_divisor     INTEGER NOT NULL DEFAULT 1,  -- 1 = every cycle, 3 = every 3rd
        max_alternatives INTEGER NOT NULL DEFAULT 0,
        enabled          INTEGER NOT NULL DEFAULT 1
    );

    -- Deduped polyline geometry; the route shape changes rarely, so it's written
    -- once per distinct shape instead of 288x/day.
    CREATE TABLE IF NOT EXISTS route_geometry (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        route_id       INTEGER NOT NULL REFERENCES routes(id),
        geom_hash      TEXT NOT NULL,      -- sha256 of the 5dp-rounded point list
        points_json    TEXT NOT NULL,      -- [[lon,lat],...] (GeoJSON order)
        bbox_json      TEXT NOT NULL,      -- [minLon,minLat,maxLon,maxLat]
        length_meters  INTEGER,
        first_seen_utc TEXT NOT NULL,
        last_seen_utc  TEXT NOT NULL,
        UNIQUE (route_id, geom_hash)
    );

    -- Poller output: one row per route (and per returned alternative) per poll.
    -- kind='historical' rows come from the departAt backfill and must NEVER be
    -- aggregated together with 'live' rows — they answer different questions.
    CREATE TABLE IF NOT EXISTS predicted_samples (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        route_id      INTEGER NOT NULL REFERENCES routes(id),
        ts_utc        TEXT    NOT NULL,
        local_date    TEXT    NOT NULL,
        local_hour    INTEGER NOT NULL,   -- 0-23 ET
        local_mod     INTEGER NOT NULL,   -- minutes since local midnight, 0-1439
        local_dow     INTEGER NOT NULL,   -- 0=Mon .. 6=Sun ET
        kind          TEXT    NOT NULL,   -- 'live' | 'historical'
        alt_index     INTEGER NOT NULL DEFAULT 0,  -- 0 = TomTom's chosen route
        travel_sec    INTEGER NOT NULL,   -- travelTimeInSeconds
        free_flow_sec INTEGER,            -- noTrafficTravelTimeInSeconds
        historic_sec  INTEGER,            -- historicTrafficTravelTimeInSeconds
        delay_sec     INTEGER,            -- trafficDelayInSeconds
        length_meters INTEGER,
        geometry_id   INTEGER REFERENCES route_geometry(id),
        sections_json TEXT                -- [{"s":i,"e":j,"mag":0-4}] traffic sections
    );
    CREATE INDEX IF NOT EXISTS idx_ps_route_hour ON predicted_samples(route_id, kind, local_hour);
    CREATE INDEX IF NOT EXISTS idx_ps_route_dow  ON predicted_samples(route_id, kind, local_dow);
    CREATE INDEX IF NOT EXISTS idx_ps_route_mod  ON predicted_samples(route_id, kind, local_mod);
    CREATE INDEX IF NOT EXISTS idx_ps_ts         ON predicted_samples(ts_utc DESC);

    -- Raw phone events. APPEND-ONLY — pairing bugs get fixed in derive, then the
    -- whole log is replayed. Ingest must never mutate or interpret.
    CREATE TABLE IF NOT EXISTS trip_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key  TEXT    NOT NULL UNIQUE,  -- client idempotency key
        kind       TEXT    NOT NULL, -- geofence_enter|geofence_leave|drive_start|drive_end
                                     -- |intent_assert|motion_segment|manual
        place_key  TEXT,
        source     TEXT    NOT NULL, -- 'ios'|'intent'|'motion_backfill'|'web_manual'
        confidence TEXT,             -- 'low'|'medium'|'high' (Core Motion) or NULL
        device_ts  INTEGER NOT NULL, -- device clock, unix epoch
        server_ts  TEXT    NOT NULL,
        local_date TEXT    NOT NULL,
        local_mod  INTEGER NOT NULL,
        local_dow  INTEGER NOT NULL,
        lat REAL, lon REAL, acc INTEGER,
        raw_json   TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_te_ts ON trip_events(device_ts);

    -- Derived trips: fully recomputable from trip_events.
    -- Which rows may enter an average is defined once, in stats.py's
    -- COUNTS_TOWARD_AVERAGES: complete, not rejected, and not still awaiting
    -- review. Never re-spell that predicate at a call site.
    CREATE TABLE IF NOT EXISTS trips (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        start_event_key     TEXT NOT NULL UNIQUE REFERENCES trip_events(event_key),
        end_event_key       TEXT UNIQUE REFERENCES trip_events(event_key),
        start_utc           TEXT NOT NULL,
        end_utc             TEXT,
        local_date          TEXT NOT NULL,
        local_hour          INTEGER NOT NULL,
        local_mod           INTEGER NOT NULL,
        local_dow           INTEGER NOT NULL,
        origin_key          TEXT,
        dest_key            TEXT,
        route_id            INTEGER REFERENCES routes(id),
        duration_sec        INTEGER,           -- set for 'complete' and 'non_commute'
        predicted_sec       INTEGER,
        predicted_sample_id INTEGER REFERENCES predicted_samples(id),
        -- 'open'|'complete'|'abandoned'|'noise'|'non_commute'
        -- non_commute = a real drive that is not one of the tracked corridors.
        -- Excluded from averages by STATUS, never by writing `verdict` — that
        -- column belongs to the human, and derive must stay free to reclassify.
        status              TEXT NOT NULL,
        verdict             TEXT NOT NULL DEFAULT 'auto',  -- 'auto'|'confirmed'|'rejected'
        needs_review        INTEGER NOT NULL DEFAULT 0,
        review_reason       TEXT,
        verdict_by          TEXT,
        derived_at          TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tr_start ON trips(start_utc DESC);
    CREATE INDEX IF NOT EXISTS idx_tr_route ON trips(route_id, status);

    -- Actually-driven polyline, one row per trip (Phase 2 iOS ingest).
    CREATE TABLE IF NOT EXISTS trip_tracks (
        trip_id       INTEGER PRIMARY KEY REFERENCES trips(id) ON DELETE CASCADE,
        points_json   TEXT NOT NULL,   -- [[lon,lat,epoch],...] simplified server-side
        bbox_json     TEXT NOT NULL,
        length_meters INTEGER,
        point_count   INTEGER
    );

    -- Which trips the completed-trip notifier has already considered.
    --
    -- A SIDE TABLE, not a trips column, and keyed on start_event_key rather
    -- than trips.id, for one reason: `derive --rebuild-all` does DELETE FROM
    -- trips and re-inserts the whole history with fresh AUTOINCREMENT ids. A
    -- marker on the trips row would be destroyed by that DELETE and the
    -- documented recovery tool would re-push every commute ever driven. This
    -- table is untouched by it.
    --
    -- A row is written when a trip is CONSIDERED, not when one is sent, so
    -- switching the notification on later does not deliver a backlog.
    CREATE TABLE IF NOT EXISTS trip_notifications (
        start_event_key TEXT PRIMARY KEY,
        considered_at   TEXT NOT NULL,
        sent_at         TEXT,
        verdict         TEXT               -- the notify.send verdict, for the log
    );

    -- APNs registration (Phase 2). apns_env matters: Xcode builds get sandbox
    -- tokens, TestFlight builds get production — sending to the wrong endpoint
    -- silently drops every push.
    CREATE TABLE IF NOT EXISTS devices (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        device_token  TEXT NOT NULL UNIQUE,
        bundle_id     TEXT NOT NULL,
        apns_env      TEXT NOT NULL,       -- 'sandbox' | 'production'
        model         TEXT, os_version TEXT, app_version TEXT,
        registered_at TEXT NOT NULL,
        last_seen_at  TEXT NOT NULL,
        enabled       INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    -- Durable API budget counter, poller-owned; the app only reads it.
    CREATE TABLE IF NOT EXISTS api_usage (
        month      TEXT PRIMARY KEY,   -- 'YYYY-MM' in ET
        calls      INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    );
"""

# Seeded once with INSERT OR IGNORE — edit live values via /settings, not here.
DEFAULT_SETTINGS = {
    "monthly_call_cap": "18000",
    "window_start_hour": "7",
    "window_end_hour": "19",
    "anomaly_min_delta_min": "10",
    "anomaly_min_live_days": "21",
    "mpg": "28",
    "fuel_price_per_gal": "3.60",
    "workdays_per_year": "240",        # legacy; cost now derives from office days
    "office_days_per_week": "3",
    "office_hours_per_day": "8.0",
    "earliest_leave_home": "07:00",
    "latest_leave_office": "18:00",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_fields(dt: datetime | None = None) -> dict:
    """Local-time bucketing fields for a UTC datetime, computed once at write time."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    loc = dt.astimezone(LOCAL_TZ)
    return {
        "local_date": loc.strftime("%Y-%m-%d"),
        "local_hour": loc.hour,
        "local_mod": loc.hour * 60 + loc.minute,
        "local_dow": loc.weekday(),  # 0=Mon
    }


def _migrate(conn):
    """Bring an existing DB up to the current `places` shape.

    CREATE TABLE IF NOT EXISTS never alters a table that already exists, so the
    two columns that turned places from three hardcoded keys into user-defined
    rows have to be added here. Both backfills read what is already on disk
    rather than assuming anyone's topology:

    - `kind` — before this column the home end WAS the row keyed 'home'. That
      was the contract, so it is the correct (and only) thing to backfill from.
    - `monitored` — inferred from the roles the routes already carry. A place
      whose corridors are all baselines is one nobody drives to any more, which
      is exactly what monitored=0 means. Guessing 1 for everything would have
      promoted those baselines to primary and tripled their poll rate.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(places)")}
    if not cols:
        return  # no places table at all — nothing to migrate (init_db has
                # already run SCHEMA by this point, so this is belt-and-braces)
    if "kind" not in cols:
        conn.execute("ALTER TABLE places ADD COLUMN kind TEXT NOT NULL DEFAULT 'office'")
        conn.execute("UPDATE places SET kind='home' WHERE key='home'")
    if "monitored" not in cols:
        conn.execute("ALTER TABLE places ADD COLUMN monitored INTEGER NOT NULL DEFAULT 1")
        conn.execute(
            """UPDATE places SET monitored=0 WHERE kind != 'home'
                 AND EXISTS (SELECT 1 FROM routes r
                             WHERE r.origin_key=places.key OR r.dest_key=places.key)
                 AND NOT EXISTS (SELECT 1 FROM routes r
                                 WHERE (r.origin_key=places.key OR r.dest_key=places.key)
                                   AND r.role='primary')""")


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        now = utcnow()
        conn.executemany(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            [(k, v, now) for k, v in DEFAULT_SETTINGS.items()],
        )


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_setting(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str):
    conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, utcnow()),
    )


def get_places(conn) -> list[dict]:
    """Home first, then offices alphabetically — every surface that lists places
    (settings cards, the manual-trip pickers) reads this order, so it is set
    once here instead of being re-sorted per template."""
    return [dict(r) for r in conn.execute(
        """SELECT * FROM places
           ORDER BY CASE kind WHEN 'home' THEN 0 ELSE 1 END, label, key""")]


def get_routes(conn, enabled_only: bool = True) -> list[dict]:
    q = "SELECT * FROM routes"
    if enabled_only:
        q += " WHERE enabled=1"
    return [dict(r) for r in conn.execute(q + " ORDER BY id")]


def find_route(conn, heading: str, role: str = "primary") -> dict | None:
    """The corridor a caller means by "the morning commute", resolved from
    (role, heading) instead of a literal slug.

    Slugs are generated from user-defined place keys now, so nothing outside
    this module may name one: 'new-out' exists only in deployments that
    predate user-defined places, and a fresh install has never heard of it.
    """
    row = conn.execute(
        """SELECT * FROM routes WHERE enabled=1 AND role=? AND heading=?
           ORDER BY id LIMIT 1""", (role, heading)).fetchone()
    return dict(row) if row else None
