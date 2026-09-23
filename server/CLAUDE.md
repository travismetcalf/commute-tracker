# commute-tracker

Measures the new-office commute against the old one. Polls TomTom for traffic-aware travel
times on a weekday daytime window, stores everything in SQLite, and serves a dashboard on
the trusted network plus publicly behind SSO. Real trips arrive from the iOS companion app
(`../ios/`, Phase 2) via `/api/v1/ingest/*`.

(Deployment specifics — hostnames, schedules, operator commands — live in `OPS.md`, which
is export-ignored and never ships in public snapshots.)

## Architecture notes (the "why")

- **Poller runs on the HOST via a scheduler (systemd user timers on the Linux deploy host
  rendered from a fleet manifest kept in a separate ops repo; the mac-era
  launchd templates are retired), not in the container** — the email notification path exists
  only on the host, and the poller must be able to report the container being
  down. The container only serves HTTP + writes trip ingest.
- **All local-time bucketing fields (`local_hour`, `local_mod`, `local_dow`, `local_date`)
  are computed in Python with `zoneinfo` at WRITE time** and stored as integers. Never use
  SQLite's `localtime` — DST is handled once, at ingest, correctly.
- **`predicted_samples.kind`**: `'live'` (real-time polls) vs `'historical'` (departAt
  backfill). Never mix them in one aggregate — they answer different questions.
- **`trip_events` is append-only**; `trips` is derived and fully recomputable
  (`derive-trips.sh --rebuild-all`). Ingest is a single INSERT that can't corrupt anything.
- **Places are user-defined rows, routes are derived from them.** `places.key` is
  slugified from the label once and then immutable (it is written into the
  append-only event log); renaming edits `label` only. `_sync_routes()` builds one
  corridor per home↔office pair per direction, matching existing rows on
  `(origin_key, dest_key)` — never on slug — so a deployment that predates this
  keeps its hand-seeded `new-out`/`old-in` slugs and its `route_id`s. `monitored`
  is the single switch: monitored → the phone geofences it and its corridors are
  `role='primary'` at full poll rate; unmonitored → `role='baseline'`, divisor 3,
  no geofence, history and coordinates kept. **Nothing outside `db.py` may name a
  slug** — resolve corridors with `find_route(conn, heading, role)`, and let the
  payload carry labels to the clients.
- **`/api/v1/*` is bearer-token gated** (the reverse proxy skips forward_auth for it; the
  app enforces `hmac.compare_digest`). Everything else rides SSO forward-auth at the edge.
- **TomTom key never reaches the browser** — map tiles are proxied via `/tiles/*`.
- API budget: hard cap in `settings.monthly_call_cap` (18000); poller exits quietly rather
  than risk the key. Do NOT drop polling below 10 min — with corridors that overruns 20k/mo.

## iOS companion (Phase 2)

Personal Automations + Geofency were rejected (no CarPlay; at decision time Shortcuts
had no Bluetooth *disconnect* trigger — iOS 26 later added one, now used for Arrived). The native app in `../ios/` does geofence wake → Core Motion
confirmation (7-day replay buffer) → route recording → durable outbox. A new phone means
re-provisioning via TestFlight; the bearer token lives in the password manager.
