# Commute — iOS companion (Phase 2)

Automatic trip capture for the commute-tracker server. Three tabs: Track
(the trip engine), Dashboard (WKWebView on the public site), Settings.

## Build

```bash
cd ios && xcodegen            # regenerates Commute.xcodeproj from project.yml
open Commute.xcodeproj        # set the signing team once, in Xcode
xcodebuild -scheme Commute -destination 'generic/platform=iOS Simulator' build
```

The build destination names no device on purpose — it is a compile-only check.
A pinned model (`name=iPhone 16 Pro`) rots the moment Xcode ships a new
simulator set and fails with "Unable to find a device matching the provided
destination specifier". To install or run rather than just compile, substitute
a device this Mac actually has: `xcrun simctl list devices available`, or
`xcodebuild -scheme Commute -showdestinations` for the same list as xcodebuild
resolves it.

The `.xcodeproj` is generated and gitignored — `project.yml` is the source of
truth. Requires full Xcode (not just Command Line Tools): install from the App
Store, then `sudo xcode-select -s /Applications/Xcode.app` and open once to
install the iOS platform.

## Architecture (the why)

Four layers, mirroring the plan:
1. **Wake** — `CLMonitor` geofences, one per place the server marks
   `monitored` (places and radii both come from `/api/v1/config`, defined at
   the server's /settings). Unmonitored places are still IN that payload —
   baseline corridors need their coordinates — so the filter lives in
   `TripEngine.armGeofences`, which is also the only place that computes the
   removal set. Significant-location-changes are the relaunch backstop; visits
   are advisory corroboration only.
2. **Confirm** — `CMMotionActivityManager.queryActivityStarting` from the last
   checkpoint. The coprocessor records ~7 days regardless of the app running,
   so missed wakes are recoverable gaps, not lost trips. Runs on EVERY wake.
3. **Record** — `CLLocationManager` polyline capture (legacy API on purpose:
   `CLLocationUpdate` has no accuracy/distanceFilter knobs). Coarse accuracy
   by default; stops on geofence-enter, 3-min stationary, or a 2h hard cap.
4. **Outbox** — raw-SQLite durable queue (`Outbox.swift`), file protection
   `completeUntilFirstUserAuthentication` (background relaunch after reboot
   must be able to write). Flushed inline on every wake + foreground +
   nightly BGProcessingTask. Idempotency keys make replays harmless.

**The server pairs events into trips, not the app** (`server/app/derive.py`).
The app only reports facts: geofence_enter/leave, intent_assert, drive_end,
motion_segment.

## Launch-critical invariants (do not "clean up")

- `TripEngine.bootstrapAtLaunch` runs SYNCHRONOUSLY in
  `didFinishLaunchingWithOptions`: iOS 18 forgets `CLServiceSession` and
  `CLMonitor` subscriptions a few seconds after a background relaunch.
- One `CLMonitor.events` subscription per instance, never resubscribed
  (cancel-and-resubscribe fails silently).
- `pausesLocationUpdatesAutomatically = false` — a pause ends background
  delivery until the user reopens the app.
- `handleFlushTask` resubmits the BG request FIRST — forgetting opts the app
  out of future runs permanently.

## Server contract

Bearer token (iOS Keychain; canonical copy in the password manager) against `/api/v1/*`:
- `GET  /api/v1/config` — places + geofence radii
- `POST /api/v1/ingest/events` — batch; `ok`/`duplicate` both mean delivered
- `POST /api/v1/ingest/track` — polyline; 409 = trip not derived yet, retry later

## Shortcuts / Bluetooth

`DriveStartedIntent` / `ArrivedIntent` give second-level precision at both
ends: Personal Automations on the car stereo's Bluetooth CONNECT → "Drive
started" and DISCONNECT → "Arrived" (the disconnect trigger is NEW in iOS 26 —
older docs claiming it doesn't exist are outdated). Geofence-enter and
stationary detection remain the backstops when an automation misses.
Personal Automations are device-local: a new phone means recreating both.

## Testing without driving

- `xcrun simctl location <udid> start ... ` replays a GPS route in the
  Simulator (drives the recorder + geofence transitions).
- Manual buttons on the Track tab exercise the same paths as the intents.
- Background-location behavior, BGTask scheduling, and battery CANNOT be
  validated in the Simulator — live with it on the real phone for a week and
  compare against manual entries on /trips before trusting it.

## Widget

`CommuteWidget/` is a WidgetKit app-extension target (small + medium). The
data element is user-configurable via long-press → Edit Widget (current
commute / 8 AM avg / 5 PM avg / today's plan). It reads the bearer token
from the SHARED keychain access group `…commute.shared` — `Keychain.swift`
migrates pre-sharing items on first read; the widget's copy of the query in
`CommuteWidget.swift` must stay in sync with those service/account names.
Refresh: 15-min timeline policy + `WidgetCenter.reloadAllTimelines()` on
dashboard loads and on every silent push.

## Watch

Two targets: `CommuteWatch` (the app) and `CommuteWatchWidget` (the
complication — circular, corner, rectangular, inline). The watch talks to the
server DIRECTLY over its own radios, so it keeps working with the phone out of
range; it is not a phone proxy.

The bearer token rides over from the phone via
`WCSession.updateApplicationContext` (`WatchSync.swift`, pushed at launch and
whenever the token or URL changes): latest-value-wins, and it survives the
watch being unreachable at send time. `WatchStore` lands it in the SHARED
keychain group `…commute.shared` — under its own service name
`…commute.watch`, so it never collides with the phone's item in that group —
which is how the complication extension gets to read it too.

**The shared group is a device luxury, not a guarantee.** A build signed "to
Run Locally" gets an EMPTY entitlements blob, so every group-scoped `SecItem`
call returns `errSecMissingEntitlement` no matter how correct `project.yml`
is. Such a build can still STORE the token app-privately, so `WatchStore.write`
falls back to that scope and returns the verdict instead of discarding it: the
app keeps working and only the complication goes dark, which is the honest
scope of such a build. `configured` is a real read-back, never a flag set from
"we asked for a write" — that distinction is what stops the status line
blaming the phone for a signing problem.

Assert buttons POST `intent_assert` / `drive_end` to `/api/v1/ingest/events` —
same append-only contract as the phone, the server still derives the trips.
The complication names its destination from the summary's `primary` map
(`dest_label`) because places are user-defined and "office" is not a word it
can assume. Timeline policy is 20 min (the watchOS budget is tight); the app's
`refresh()` calls `reloadAllTimelines()` so a fresh install doesn't sit at "—"
for a whole cycle.

`Shared/` compiles into BOTH the phone and the watch — keep anything added
there free of UIKit/WatchKit.

```bash
xcodebuild -scheme CommuteWatch -destination 'generic/platform=watchOS Simulator' build
```

Only needed to iterate on the watch alone: `CommuteWatch` is an embedded
dependency of `Commute`, so the iOS build and `scripts/testflight.sh` already
carry it (`SKIP_INSTALL: YES` is what keeps the archive correct).

## Push

App side is fully wired: permission prompt at launch, APNs registration,
token POSTed to `/api/v1/devices` with `apns_env` = sandbox for `#if DEBUG`
(Xcode installs) / production (TestFlight). Silent pushes (server sends one
per poll cycle) reload widget timelines and run a reconcile+flush — this
requires `remote-notification` in UIBackgroundModes; don't remove it.
Server side is `server/app/push.py`; it no-ops until the APNs `.p8` key is
configured in `server/.env` (see `.env.example` — the key is created ONCE at
developer.apple.com, lives in the password manager, never in git).

## TestFlight

```bash
scripts/testflight.sh   # archive + upload, build number = git commit count
```

One-time prereq: an App Store Connect app record for
`com.travismetcalf.commute` (ASC web UI only — no CLI exists for record
creation). Internal testers skip Beta App Review; builds expire after 90
days, so re-upload roughly quarterly.

## Not built (deliberately)

Live Activities — highest effort, and the useful window is exactly when the
user shouldn't be looking at a phone.
