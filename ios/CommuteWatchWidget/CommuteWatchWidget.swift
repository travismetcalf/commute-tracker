import WidgetKit
import SwiftUI
import Security

// ── data (self-contained: extensions can't see app-target types) ──────────

struct WatchWidgetPayload: Codable {
    struct Bucket: Codable { let avg_sec: Int? }
    struct Now: Codable { let travel_sec: Int?; let historic_sec: Int? }
    /// The server's own name for where this heading ends — places are
    /// user-defined, so "office" is not a word this complication can assume.
    /// Optional: older servers omit it, and a complication must still decode.
    struct Route: Codable { let dest_label: String? }
    let direction: String
    let now: Now?
    let am_out: Bucket?
    let pm_in: Bucket?
    let primary: [String: Route?]?

    var destination: String? { (primary?[direction] ?? nil)?.dest_label }
}

enum ComplicationAPI {
    static func read(_ account: String) -> String? {
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "com.travismetcalf.commute.watch",
            kSecAttrAccount as String: account,
            kSecAttrAccessGroup as String: "8F62W24942.com.travismetcalf.commute.shared",
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func fetch() async -> WatchWidgetPayload? {
        guard let token = read("token"),
              let url = URL(string: (read("serverURL") ?? "https://traffic.travismetcalf.com")
                            + "/api/v1/summary") else { return nil }
        var req = URLRequest(url: url, timeoutInterval: 12)
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return try? JSONDecoder().decode(WatchWidgetPayload.self, from: data)
    }
}

// ── timeline ──────────────────────────────────────────────────────────────

struct WatchEntry: TimelineEntry {
    let date: Date
    let payload: WatchWidgetPayload?
}

struct WatchProvider: TimelineProvider {
    func placeholder(in context: Context) -> WatchEntry { WatchEntry(date: .now, payload: nil) }

    func getSnapshot(in context: Context, completion: @escaping (WatchEntry) -> Void) {
        Task { completion(WatchEntry(date: .now, payload: await ComplicationAPI.fetch())) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<WatchEntry>) -> Void) {
        Task {
            let entry = WatchEntry(date: .now, payload: await ComplicationAPI.fetch())
            // Complication budget is tight on watchOS; 20 min is a fair trade
            // between freshness and the system throttling us.
            completion(Timeline(entries: [entry],
                                policy: .after(.now.addingTimeInterval(20 * 60))))
        }
    }
}

// ── views per family ──────────────────────────────────────────────────────

struct ComplicationView: View {
    var entry: WatchEntry
    @Environment(\.widgetFamily) private var family

    private var mins: String {
        guard let s = entry.payload?.now?.travel_sec else { return "—" }
        return "\(Int((Double(s) / 60).rounded()))"
    }
    /// Complication real estate is the tightest on any surface, so this is an
    /// arrow plus the destination's own name — truncated by the system if the
    /// user picked a long one, which still beats naming the wrong place.
    private var dirShort: String {
        guard let p = entry.payload else { return "→" }
        if let d = p.destination { return "→ \(d)" }
        return p.direction == "outbound" ? "→ out" : "→ back"
    }
    private var delta: String {
        guard let live = entry.payload?.now?.travel_sec,
              let typ = entry.payload?.now?.historic_sec else { return "" }
        let d = Int((Double(live - typ) / 60).rounded())
        return d > 2 ? "+\(d) vs usual" : d < -2 ? "\(-d) quicker" : "about usual"
    }

    var body: some View {
        switch family {
        case .accessoryCircular:
            VStack(spacing: -2) {
                Text(mins).font(.system(size: 20, weight: .bold, design: .serif))
                Text("min").font(.system(size: 10)).opacity(0.7)
            }
            .containerBackground(for: .widget) { Color.clear }
        case .accessoryCorner:
            Text("\(mins)m")
                .font(.system(size: 18, weight: .bold, design: .serif))
                .widgetLabel { Text(dirShort) }
                .containerBackground(for: .widget) { Color.clear }
        case .accessoryInline:
            Text("\(dirShort) \(mins) min")
                .containerBackground(for: .widget) { Color.clear }
        default: // accessoryRectangular
            VStack(alignment: .leading, spacing: 0) {
                Text(dirShort.uppercased())
                    .font(.system(size: 11, weight: .semibold)).opacity(0.7)
                HStack(alignment: .firstTextBaseline, spacing: 2) {
                    Text(mins).font(.system(size: 24, weight: .bold, design: .serif))
                    Text("min").font(.system(size: 12)).opacity(0.7)
                }
                Text(delta).font(.system(size: 11)).opacity(0.7)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .containerBackground(for: .widget) { Color.clear }
        }
    }
}

// ── bundle ────────────────────────────────────────────────────────────────

@main
struct CommuteWatchWidgetBundle: WidgetBundle {
    var body: some Widget { CommuteComplication() }
}

struct CommuteComplication: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "CommuteComplication", provider: WatchProvider()) { entry in
            ComplicationView(entry: entry)
        }
        .configurationDisplayName("Commute")
        .description("Current commute time at a glance.")
        .supportedFamilies([.accessoryCircular, .accessoryCorner,
                            .accessoryRectangular, .accessoryInline])
    }
}
