import Foundation

// Codable mirror of /api/v1/dashboard. Field names match the server JSON.

/// Which corridor a heading actually means, named by the server. Places are
/// user-defined, so their route slugs are too — this is how the app learns
/// which slug keys `now`/`routes`/`alts` without knowing any of them up front.
struct PrimaryRoute: Codable {
    let slug: String
    let label: String
    let origin_label: String?
    let dest_label: String?
}

struct DashboardPayload: Codable {
    let headline: Headline
    let plan: Plan
    let cost: Cost
    let bias: Bias
    let now: [String: NowSample?]
    let routes: [String: RouteStats]
    /// The outbound/inbound corridors this payload is about. Optional so a
    /// server caught mid-upgrade degrades to an empty-state card rather than
    /// failing the whole decode — never to a guessed slug, which is the
    /// assumption this field exists to remove.
    let primary: [String: PrimaryRoute?]?
    /// Per-corridor predictability, keyed by route slug.
    /// Optional: older servers omit it.
    let reliability: [String: Reliability]?
    /// Live time per corridor, keyed exactly like `now` — `alts[slug][0]` is
    /// the same corridor as `now[slug]`. Optional for the same reason
    /// `reliability` is: a new build must still render against an old server.
    let alts: [String: [AltLive]]?
}

/// The newest live reading for ONE corridor. This is the record the map labels
/// its lines with, so a pill and a NOW row are the same number by construction.
///
/// Only the two fields a row cannot exist without are required. The rest stay
/// optional so a server caught mid-upgrade degrades to a thinner row instead of
/// failing the whole dashboard decode.
struct AltLive: Codable, Identifiable {
    /// 0 = primary. The key for color and label — NEVER the array position: a
    /// corridor with no live sample is absent, so [0, 2] is a legal array.
    let alt_index: Int
    let travel_sec: Int
    let free_flow_sec: Int?
    let historic_sec: Int?
    let delay_sec: Int?
    let length_meters: Int?
    let ts_utc: String?
    let stale: Bool?
    var id: Int { alt_index }
}

/// Which corridor is the PREDICTABLE one — buffer (p90 − p50) is the slack
/// you must budget beyond the median; `steadiest` indexes into `corridors`.
struct Reliability: Codable {
    struct Corridor: Codable {
        let mi: Double
        let n: Int
        let p50_sec: Int?
        let p90_sec: Int?
        let buffer_sec: Int?
    }
    let corridors: [Corridor]
    let steadiest: Int?
}

struct Bucket: Codable {
    let n: Int
    let avg_sec: Int?
    let p10_sec: Int?
    let p50_sec: Int?
    let p90_sec: Int?
    let source: String?
    let hour: Int?
    let dow: Int?
    let slot30: Int?
    /// Only present when a bucket fell back to the historical profile: `n` then
    /// counts historical rows and this counts the (too few) live ones. The
    /// collection-health footer has to add both or it undercounts badly.
    let live_n: Int?
}

struct DepartBucket: Codable {
    let n: Int
    let avg_sec: Int?
    let p50_sec: Int?
    let p90_sec: Int?
    let source: String
    let dots: [Int]?
}

struct Multiplier: Codable { let ratio: Double; let delta_sec: Int }

struct Headline: Codable {
    /// What to call these numbers. `am_out`/`pm_in` are the two corridors'
    /// labels; the two `*_place` fields name the places being compared, which
    /// is what the damage card contrasts. Optional: older servers omit it.
    struct Labels: Codable {
        let am_out: String?
        let pm_in: String?
        let primary_place: String?
        let baseline_place: String?
    }
    let am_out: DepartBucket?
    let pm_in: DepartBucket?
    let am_multiplier: Multiplier?
    let pm_multiplier: Multiplier?
    let labels: Labels?
}

/// Mirror of /api/v1/summary — the compact payload the widget and the
/// query intents share (the widget keeps its own copy; extensions can't
/// see app-target types).
struct SummaryPayload: Codable {
    struct Now: Codable { let travel_sec: Int?; let historic_sec: Int? }
    let direction: String
    let now: Now?
}

struct Plan: Codable {
    struct Day: Codable, Identifiable {
        let dow: Int
        let leave_home_mod: Int
        let leave_office_mod: Int
        let out_sec: Int
        let in_sec: Int
        let total_sec: Int
        let source: String
        let pick: Bool
        var id: Int { dow }
    }
    struct Window: Codable {
        let earliest_mod: Int
        let latest_mod: Int
        let office_hours: Double
    }
    let days: [Day]
    let picks: Int
    let window: Window?
}

struct Cost: Codable {
    struct Block: Codable { let day_min: Int; let hrs_week: Double; let hrs_year: Int }
    let new: Block?
    let old: Block?
    let fuel_year_new: Int?
    let office_days_per_week: Double?
}

struct Bias: Codable {
    struct Point: Codable, Identifiable {
        let predicted_sec: Int
        let actual_sec: Int
        var id: String { "\(predicted_sec)-\(actual_sec)" }
    }
    let n: Int
    let mean_error_sec: Int?
    let points: [Point]
}

struct NowSample: Codable {
    let travel_sec: Int?
    let free_flow_sec: Int?
    let historic_sec: Int?
    let delay_sec: Int?
    let ts_utc: String?
}

struct RouteStats: Codable {
    let hourly: [Bucket]
    let dow: [Bucket]
    let heatmap: [Bucket]
    let actual_hourly: [Bucket]
}

// ── formatting helpers shared by the native views ─────────────────────────

enum Fmt {
    static func min(_ sec: Int?) -> String {
        guard let sec else { return "—" }
        let m = Int((Double(sec) / 60).rounded())
        return m > 99 ? "\(m / 60) h \(m % 60) m" : "\(m) min"
    }
    static func bareMin(_ sec: Int?) -> String {
        guard let sec else { return "—" }
        return "\(Int((Double(sec) / 60).rounded()))"
    }
    static func hhmm(_ mod: Int) -> String {
        let h = mod / 60, m = mod % 60
        let h12 = h % 12 == 0 ? 12 : h % 12
        return String(format: "%d:%02d %@", h12, m, h < 12 ? "AM" : "PM")
    }
    /// Compact axis/row label for a 24h hour: 7a, 12p, 5p.
    static func hourLabel(_ hour: Int) -> String {
        hour < 12 ? "\(hour)a" : hour == 12 ? "12p" : "\(hour - 12)p"
    }

    /// The server stamps ts_utc as "%Y-%m-%dT%H:%M:%SZ" — plain ISO 8601, no
    /// fractional seconds, which is exactly what the default parser expects.
    private static let iso = ISO8601DateFormatter()

    static func minutesAgo(_ tsUTC: String?) -> Int? {
        guard let tsUTC, let d = iso.date(from: tsUTC) else { return nil }
        return Swift.max(0, Int(Date().timeIntervalSince(d) / 60))
    }
    static func ago(minutes m: Int) -> String {
        if m < 1 { return "just now" }
        if m < 60 { return "\(m) min ago" }
        let h = m / 60
        return h < 24 ? "\(h) h ago" : "\(h / 24) d ago"
    }
    static let dowNames = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    static let dowShort = ["Mon", "Tue", "Wed", "Thu", "Fri"]
}
