# iOS UX backlog — from the 2026-08-03 four-lens review

Status legend: ✅ done · 🔨 overridden by owner preference · ⬜ open

Curation notes (post-review):
- **#1 app icon — ✅ done** during the review run (serif C + crimson dot, Design/make-appicon.py).
- **#4 motion-permission flow — ✅ done** (checkpoint burn fixed, dedicated prompt path).
- **#5 delete swipe gesture — 🔨 overridden**: Travis explicitly requested tab swipes.
  Keep, but adopt the refinements (haptic on switch; revisit clamp if a 4th tab lands).
- **#14 retryable config sync — ✅ done** (token save / successful test / foreground-when-
  unarmed all re-arm geofences now).
- Tab background inconsistency (found by Travis, not the review) — ✅ done.
- Everything else below is open, in the review's priority order.

---

# Commute iOS — Consolidated UX Enhancement List

*40 findings merged into 27 items (13 duplicates/overlaps collapsed; the iCloud-symbol note addressed to the in-flight Recent-events redesign was dropped). Ranked by user value ÷ effort within each tier.*

## Quick wins (do next session)

1. **Add an app icon.** No Assets.xcassets exists anywhere under `ios/` and `project.yml` never sets `ASSETCATALOG_COMPILER_APPICON_NAME`, so the app ships the blank placeholder grid — the most visible polish gap; a Signal crimson-on-dusk 1024pt mark (light/dark/tinted) is picked up automatically once the catalog is in the sources folder.

2. **Apply the Signal theme app-wide.** `Theme.swift` defines the crimson accent but no `.tint` is ever set, so Track and Settings render stock system blue with raw `.green/.orange/.red` (`Track/TrackView.swift` lines 41, 68, 115; `Settings/SettingsView.swift` line 112); one `.tint(Theme.current(scheme).accent)` at the `ContentView.swift` root plus swapping in the `t.good`/`t.bad` tokens unifies the two-apps-in-one feel.

3. **Stop NowCard presenting hours-old samples as live.** `Dashboard/DashboardView.swift` renders `travel_sec` with no freshness (decoded `ts_utc` in `DashboardModels.swift` line 90 is never shown), fetches only when `payload == nil` (line 72), and the `if let p = payload` ordering makes the error branch unreachable after first load. Render age from `ts_utc`, tint when older than the 10-min poll cadence, refetch on appear/active, and overlay refresh failures instead of suppressing them.

4. **Fix the motion-permission flow.** `bootstrapAtLaunch` triggers the Motion & Fitness dialog uncontextualized at first frame (`Core/TripEngine.swift` lines 61, 123-133), the "Enable motion" button (`TrackView.swift` lines 46-49) silently no-ops behind the 120s reconcile guard, and line 154 advances `lastCheckpoint` even pre-consent — burning the 7-day history safety net. Guard the query on `authorizationStatus == .authorized`, give the button a dedicated request path, and only advance the checkpoint when authorized.

5. **Delete the custom cross-tab swipe gesture.** The `simultaneousGesture` on the whole TabView (`App/ContentView.swift` lines 20-31) fights the segmented picker, future list swipes, and edge-back gestures, isn't iOS idiom, snaps with no animation or haptic, and its `min(tab + 1, 2)` clamp breaks on a fourth tab.

6. **Make hero numerals respect Dynamic Type.** `DashboardView.swift` hardcodes `.system(size:)` 40/52/56 (lines 112, 167, 225, 234) which never scale up; replace with `@ScaledMetric`/text styles, and wrap HeroPair's forced 2-up HStack (lines 153-157) in `ViewThatFits` for accessibility sizes.

7. **Label or group the direction-scoped cards.** The Office/Home picker drives only 4 of 8 cards but direction-independent cards are interleaved among them (`DashboardView.swift` lines 44-54) with no context stamps; group the scoped trend cards under the picker or echo the direction in affected subtitles, as the web does via `[data-context]`.

8. **Sync-status truthfulness bundle.** Persist `AppState.lastSync` via `@AppStorage` (`App/AppState.swift` line 55), count unsent tracks in "Pending upload" (`Core/Outbox.swift` ~line 240), swap frozen `.formatted(.relative)` strings for self-updating `Text(sync, style: .relative)` (`TrackView.swift` line 72, `SettingsView.swift` line 99), and clear sticky `lastError` on any successful round-trip (`Outbox.swift` lines 124-149) so fixed problems stop showing as broken.

9. **Dotplot: decode hint, clipping, VoiceOver.** `DotplotView` (`DashboardView.swift` lines 187-210) has no endpoint labels or "count from the right" hint, overdraws stacks past four dots high, and as a raw Canvas is invisible to VoiceOver. Add min/max labels plus a shared caption, shrink radius when stacks exceed the height, and attach `.accessibilityLabel`/`.accessibilityValue` summarizing count/range/median.

10. **Fix the two contrast failures.** White text on heat step 3 orange #E78530 is 2.7:1 (`DashboardView.swift` ~line 451 with `Theme.swift`) — switch steps 1-3 to dark ink; PlanCard's white-on-`t.good` GO badge is 2.2:1 in dark mode (~line 293) — use `t.good` on `t.goodTint` per the delta-chip idiom.

11. **Add haptics at trip-state moments.** Zero `.sensoryFeedback` anywhere; the glance-free-in-a-car "I'm driving now"/"I've arrived" buttons (`TrackView.swift` lines 84-93) most need it, plus a success tick on Settings' "Test connection" (`SettingsView.swift` line 48) and a `.symbolEffect(.pulse)` on the recording glyph.

12. **Confirm destructive token removal.** The footnote-sized "Remove" button (`SettingsView.swift` lines 33-36) instantly nils the Keychain token, killing all sync in one accidental tap; promote it to its own row and gate behind `confirmationDialog("Remove API token?")`.

13. **Align low-confidence gating with the web.** Caption the undecodable 0.55-opacity dimmed hero (`DashboardView.swift` line 169), decode `old_am_out`/`old_pm_in` in `DashboardModels.swift` so DamageCard mirrors the web's dual n>=5 gate, and use `bias.n` for a countdown in ModelCheckCard's interim copy.

## Meaningful upgrades

14. **Make config sync retryable — token save should arm geofences.** `attachMonitor` fetches `/api/v1/config` exactly once and discards failure via `try?` (`Core/TripEngine.swift` lines 65-98), so finishing setup leaves "Geofences armed: 0" until relaunch. Extract a retryable config-sync called after token save, after a successful Test connection, and on foreground when unarmed — and have "Save token" (`SettingsView.swift` lines 40-47) run the connection check inline plus explain its 32-character enable condition.

15. **Replace the last-event log with a real engine-health model.** `engineStatus` is a write-only string that resets on relaunch, never updates on arrival or auto-stop, and claims "recording" even when `RouteRecorder.start` silently bails on non-Always auth (`TripEngine.swift` lines 178-187, `Core/RouteRecorder.swift` line 36). Derive an armed/recording/degraded/stale enum from `monitorsArmed` + `isRecording` + newest outbox event age, color it with the traffic ramp, and surface "last event captured N ago" — directly targeting the project's named "quietly stopped" failure mode.

16. **Overhaul error and empty states with routing.** Dashboard's failure card blames the token for every failure with no recovery path (`DashboardView.swift` lines 56-62), a missing token is invisible on Track (`TripEngine.swift` line 71 swallows it; `Outbox.swift` line 124 never phones home when empty), and empty states are bare footnotes. Branch copy by error type (offline/401/5xx/no-token), adopt `ContentUnavailableView` with Retry and "Open Settings" actions, and lift tab selection out of `ContentView.swift` into AppState so error states can actually route.

17. **Stop the hourly chart fabricating trends, and key its series.** `HourlyCard` (`DashboardView.swift` lines 322-355) filters to n>=3 then draws straight bridges across the gaps the web explicitly refuses (`spanGaps:false`); break the line into per-run series, and add a compact legend distinguishing predicted median from actual-trip lavender dots.

18. **Chart and heatmap accessibility pass.** Give model-check points a shape channel via `.symbol(by:)` instead of red/green alone (~line 489), label heatmap cells "Tuesday 8:00, 42 minutes" with empties hidden (~line 442), and rename chart `.value` labels from "p10"/"median"/"a" to spoken English across `DashboardView.swift`.

19. **Restore the Plan/Damage decision-support layer.** PlanCard (lines 275-313) dropped per-leg durations, the source tag, and the spread payoff footer (`spread_sec` isn't even in the Codable model in `DashboardModels.swift`); DamageCard lost the old-vs-new bullet bars and comparison context that anchor the naked "1.8×" — the web answers "so what," iOS only answers "what."

20. **Make the Bluetooth automation discoverable and verifiable.** The app's highest-precision signal exists only as an intent description in `Intents/DriveIntents.swift`; add a Settings section with the step-by-step recipe, a `shortcuts://` link, a last-intent-fired timestamp from the outbox, and a never-fired warning — it must also be recreated per-device, which nothing mentions.

21. **Surface the review queue on the capturing device.** Confirm/reject exists only on the desktop Trips page although `/api/v1/trips` is already on the bearer surface (`server/app/main.py` line 655); minimally, add `review_count` to the dashboard payload and show a "2 trips need review" chip near ModelCheckCard.

22. **Animate dashboard state changes.** Payload arrival and direction toggles hard-swap every card in one frame; add `.animation(.snappy, value: dir)` and `.redacted(.placeholder)` skeleton cards in place of the bare spinner in `DashboardView.swift`.

23. **First-run setup checklist.** The 4-step setup (token, location Always, motion, Bluetooth Shortcut) is scattered across two tabs and the repo docs with nothing sequencing it; a checklist card atop Track with live done/undone state in dependency order also gives regressed steps a durable home — items 14-16 above shrink this but don't replace it.

## Later / nice-to-have

24. **Route-right-now on the phone.** Cheap first step: NowCard already decodes `delay_sec`/`free_flow_sec` (`DashboardModels.swift` lines 86-88) and ignores them — add the typical/free-flow/delay rows; the purpose-built `/api/v1/route` GeoJSON endpoint (`main.py` line 648) then feeds the larger MapKit polyline card.

25. **Toolbar-hosted direction picker.** The scope control scrolls off-screen as the first ScrollView row (`DashboardView.swift` lines 22, 76-82); `ToolbarItem(placement: .principal)` keeps it reachable and gets Liquid Glass treatment free on iOS 26.

26. **Honest empty states for WeekdayCard and HeatmapCard.** WeekdayCard draws bare axes when all buckets have n<3 (lines 376-395) and HeatmapCard renders placeholder grids uncaptioned (lines 399-458); mirror HourlyCard's "No data yet." with the unlock condition stated ("appears after 3 trips per weekday").

27. **Small-copy and idiom sweep** (one batch): rename the Dashboard tab/title mismatch ("Commute" vs "Dashboard", `DashboardView.swift` line 70 / `ContentView.swift` line 11); fix SF symbol semantics (`pause.circle` for idle, `wifi.exclamationmark` for non-network failures — `TrackView.swift` lines 67, 75, `DashboardView.swift` line 58); add `.badge(state.pendingEvents)` to the Track tab; format times via `Date.FormatStyle` instead of hardcoded AM/PM (`DashboardModels.swift` lines 112-116, `DashboardView.swift` lines 362, 429); make the GPS-accuracy picker segmented like Theme (`SettingsView.swift` lines 62-88); de-jargon "step 1 of 2" / "Re-run reconciliation" copy (`TrackView.swift` lines 33, 47, `SettingsView.swift` line 68); add the offline reassurance line ("queued — sends automatically") beside the pending counter (`TrackView.swift` line 70); retitle App Intents to "Start Drive"/"Log Arrival" with spoken dialogs (`DriveIntents.swift`); add the minor VoiceOver labels for icon-only Recording/sent status and arrow-glyph segments (`TrackView.swift` lines 66, 114, `DashboardView.swift` lines 78-79). ~~Take route labels from the payload instead of hardcoded "new-out"/"new-in" strings~~ — DONE, in the user-defined-places change: every surface now reads `primary`/`labels` off the payload.