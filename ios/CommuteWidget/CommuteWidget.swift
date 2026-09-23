import WidgetKit
import SwiftUI
import AppIntents
import Security

// ── configuration: which data element the widget shows ────────────────────

enum WidgetMetric: String, AppEnum {
    case now, morning, evening, plan

    static let typeDisplayRepresentation = TypeDisplayRepresentation(name: "Data element")
    static let caseDisplayRepresentations: [WidgetMetric: DisplayRepresentation] = [
        .now: "Current commute",
        .morning: "8 AM average",
        .evening: "5 PM average",
        .plan: "Today's plan",
    ]
}

struct CommuteWidgetConfig: WidgetConfigurationIntent {
    static let title: LocalizedStringResource = "Commute Widget"
    static let description = IntentDescription("Choose which commute number to show.")

    @Parameter(title: "Show", default: .now)
    var metric: WidgetMetric
}

// ── data ──────────────────────────────────────────────────────────────────

struct WidgetPayload: Codable {
    struct Bucket: Codable { let avg_sec: Int?; let p90_sec: Int?; let n: Int; let source: String }
    struct Now: Codable { let travel_sec: Int?; let historic_sec: Int?; let ts_utc: String? }
    struct Mult: Codable { let ratio: Double }
    /// Where each half-day is headed, named by the server. Places are
    /// user-defined, so "the office" is not a thing this extension can assume.
    /// Optional: older servers omit it, and a widget must never fail to decode.
    struct Route: Codable { let dest_label: String? }
    let direction: String
    let now: Now?
    let am_out: Bucket?
    let pm_in: Bucket?
    let am_multiplier: Mult?
    let primary: [String: Route?]?

    /// A short destination for a heading, or nil when the server is older or
    /// has no corridor there. Widget rows are narrow — the destination alone,
    /// never the full "A → B" route label.
    func destination(_ heading: String) -> String? { (primary?[heading] ?? nil)?.dest_label }
}

enum WidgetAPI {
    /// The widget reads the same shared-keychain token as the app.
    static func token() -> String? {
        var q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "com.travismetcalf.commute",
            kSecAttrAccount as String: "api-bearer-token",
            kSecAttrAccessGroup as String: "8F62W24942.com.travismetcalf.commute.shared",
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func fetch() async -> WidgetPayload? {
        guard let token = token(),
              let url = URL(string: "https://traffic.travismetcalf.com/api/v1/summary") else { return nil }
        var req = URLRequest(url: url, timeoutInterval: 12)
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return try? JSONDecoder().decode(WidgetPayload.self, from: data)
    }
}

// ── timeline ──────────────────────────────────────────────────────────────

struct Entry: TimelineEntry {
    let date: Date
    let metric: WidgetMetric
    let payload: WidgetPayload?
}

struct Provider: AppIntentTimelineProvider {
    func placeholder(in context: Context) -> Entry {
        Entry(date: .now, metric: .now, payload: nil)
    }

    func snapshot(for configuration: CommuteWidgetConfig, in context: Context) async -> Entry {
        Entry(date: .now, metric: configuration.metric, payload: await WidgetAPI.fetch())
    }

    func timeline(for configuration: CommuteWidgetConfig, in context: Context) async -> Timeline<Entry> {
        let entry = Entry(date: .now, metric: configuration.metric,
                          payload: await WidgetAPI.fetch())
        // Refresh roughly with the server's 10-min poll cadence; iOS budgets
        // the real rate (~15–60 min). A future silent push tightens this.
        return Timeline(entries: [entry],
                        policy: .after(.now.addingTimeInterval(15 * 60)))
    }
}

// ── views ─────────────────────────────────────────────────────────────────

struct WidgetView: View {
    var entry: Entry
    @Environment(\.widgetFamily) private var family

    private let dusk = Color(red: 0.059, green: 0.063, blue: 0.125)
    private let ink = Color(red: 0.925, green: 0.914, blue: 0.961)
    private let faint = Color(red: 0.553, green: 0.537, blue: 0.667)
    private let crimson = Color(red: 0.878, green: 0.282, blue: 0.373)

    var body: some View {
        content
            .containerBackground(dusk, for: .widget)
    }

    @ViewBuilder private var content: some View {
        if let p = entry.payload {
            VStack(alignment: .leading, spacing: 4) {
                Text(title(p)).font(.caption2.weight(.semibold))
                    .foregroundStyle(faint).textCase(.uppercase).kerning(0.8)
                Text(value(p))
                    .font(.system(size: family == .systemSmall ? 34 : 40,
                                  weight: .bold, design: .serif))
                    .minimumScaleFactor(0.5).lineLimit(1)
                    .foregroundStyle(ink)
                Text(subtitle(p)).font(.caption2).foregroundStyle(faint).lineLimit(2)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        } else {
            VStack(spacing: 4) {
                Image(systemName: "car.fill").foregroundStyle(crimson)
                Text("Open Commute to connect")
                    .font(.caption2).foregroundStyle(faint)
                    .multilineTextAlignment(.center)
            }
        }
    }

    private func min(_ sec: Int?) -> String {
        guard let sec else { return "—" }
        return "\(Int((Double(sec) / 60).rounded()))"
    }

    private func title(_ p: WidgetPayload) -> String {
        switch entry.metric {
        case .now: p.destination(p.direction).map { "To \($0) now" }
                   ?? (p.direction == "outbound" ? "Outbound now" : "Heading back now")
        case .morning: "8 AM average"
        case .evening: "5 PM average"
        case .plan: "Today"
        }
    }

    private func value(_ p: WidgetPayload) -> String {
        switch entry.metric {
        case .now: "\(min(p.now?.travel_sec)) min"
        case .morning: "\(min(p.am_out?.avg_sec)) min"
        case .evening: "\(min(p.pm_in?.avg_sec)) min"
        case .plan: "\(min((p.am_out?.avg_sec).flatMap { a in (p.pm_in?.avg_sec).map { a + $0 } })) min"
        }
    }

    private func subtitle(_ p: WidgetPayload) -> String {
        switch entry.metric {
        case .now:
            if let live = p.now?.travel_sec, let typ = p.now?.historic_sec {
                let d = Int((Double(live - typ) / 60).rounded())
                return d > 2 ? "+\(d) min vs usual" : d < -2 ? "\(-d) min quicker" : "about usual"
            }
            return "live traffic"
        case .morning:
            return "\(p.destination("outbound").map { "→ \($0)" } ?? "outbound")"
                 + " · p90 \(min(p.am_out?.p90_sec)) min"
        case .evening:
            return "\(p.destination("inbound").map { "→ \($0)" } ?? "back")"
                 + " · p90 \(min(p.pm_in?.p90_sec)) min"
        case .plan: return "round trip at your usual times"
        }
    }
}

// ── widget bundle ─────────────────────────────────────────────────────────

@main
struct CommuteWidgetBundle: WidgetBundle {
    var body: some Widget {
        CommuteWidget()
    }
}

struct CommuteWidget: Widget {
    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: "CommuteWidget",
                               intent: CommuteWidgetConfig.self,
                               provider: Provider()) { entry in
            WidgetView(entry: entry)
        }
        .configurationDisplayName("Commute")
        .description("Your commute at a glance — long-press to pick which number.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}
