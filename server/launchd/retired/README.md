# Retired launchd templates

Host schedules moved to systemd user timers on the Linux deploy host on 2026-09-05
(rendered from a fleet manifest in a separate ops repo; see OPS.md "Host schedules"). `commute-tracker-mdns` — the last macOS-host job — went with the mac edge
on 2026-09-06 (`traffic.local` retired; the app is public at `traffic.travismetcalf.com` through the
Linux host's edge). Parked here 2026-09-08. **`scripts/install.sh` no longer renders these; do not load them.**

| Template | Replaced by (fleet manifest job) |
|---|---|
| `commute-tracker-poll.plist.template` | `commute-poll` — every 10 min |
| `commute-tracker-derive.plist.template` | `commute-derive` — every 15 min |
| `commute-tracker-brief.plist.template` | `commute-tracker-brief` — Mon–Fri 07:15 |
| `commute-tracker-smoke.plist.template` | `commute-smoke` — daily 09:55 |
| `commute-tracker-backup.plist.template` | `commute-tracker-backup` — daily 03:20 |
| `commute-tracker-restore-test.plist.template` | `commute-restore-test` — Mon 11:00 |
| `commute-tracker-mdns.plist.template` | nothing — `traffic.local` retired 2026-09-06 |
