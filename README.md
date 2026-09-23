# commute-tracker

Did the new office really double the commute? This measures it.

A self-hosted commute predictor: a FastAPI server polls real corridor timings
around the clock, learns the shape of each day, and answers the only two
questions that matter — **which office days cost the least**, and **when to
leave**. A native iOS companion records actual drives in the background, so
the model is continuously checked against real trips instead of trusted on
faith.

**The full story, with screenshots:**
[travismetcalf.com/projects/commute-tracker](https://travismetcalf.com/projects/commute-tracker/)

## What it does

- **Live prediction** with typical / free-flow / delay breakdown per direction
- **When to leave** — median + 10th–90th percentile bands per departure time,
  built from months of sampled history (quantiles, not a vendor's black box)
- **Plan your week** — ranks office days inside your time window and marks the
  GO days
- **Departure heatmap** — median commute by weekday × hour
- **The damage** — old commute vs new, measured the same way, priced in hours
  and fuel per year
- **iOS app + home-screen widget + watch app** — the same answers native, plus
  background trip recording (battery-friendly coarse GPS) that feeds the
  model-check loop

## Layout

- [`server/`](server/CLAUDE.md) — FastAPI + SQLite poller, prediction engine,
  and the web dashboard (vanilla JS, Chart.js, MapLibre)
- [`ios/`](ios/CLAUDE.md) — SwiftUI app, widget, and watch app; the Xcode
  project is generated with [XcodeGen](https://github.com/yonaskolb/XcodeGen)
  from `project.yml`

The `CLAUDE.md` files are the real operating docs — written for AI-assisted
development, kept brutally honest (gotchas, foot-guns, and postmortems
included).

## Quickstart

Server:

```bash
cd server
cp .env.example .env    # add your TomTom API key (free tier)
bash scripts/install.sh
```

iOS app:

```bash
cd ios
xcodegen generate       # brew install xcodegen
open Commute.xcodeproj
```

Point the app at your server URL in Settings, paste the bearer token the
installer generated, and drive.

## Security posture

Reads on the trusted network are free; every write lives behind SSO + MFA on
the public surface; the app API uses bearer auth with constant-time
comparison. The reasoning is documented in the
[patterns](https://travismetcalf.com/patterns/) this project follows.

## License

[MIT](LICENSE)
