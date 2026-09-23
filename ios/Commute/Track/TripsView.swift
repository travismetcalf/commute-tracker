import SwiftUI

// ── mirror of /api/v1/trips ───────────────────────────────────────────────

struct TripRow: Codable, Identifiable {
    let id: Int
    let local_date: String
    let local_mod: Int
    /// Optional purely defensively: this field has always been in the payload,
    /// but one missing key would fail the decode for the WHOLE list, and a
    /// blank trips tab is a worse failure than a slightly coarser sort.
    let start_utc: String?
    let duration_sec: Int?
    let predicted_sec: Int?
    let status: String
    let verdict: String
    let needs_review: Int
    let review_reason: String?
    let route_slug: String?
    let origin_key: String?
    let dest_key: String?
    /// Names resolved server-side from the places table. The app used to
    /// string-substitute one deployment's place keys into English, which meant
    /// any other deployment's trips rendered as raw keys. Optional: a place
    /// deleted since the trip was recorded has no label left to join to.
    let route_label: String?
    let origin_label: String?
    let dest_label: String?

    var routeLabel: String {
        if let route_label { return route_label }
        // No route matched (an unpaired or one-ended trip) — name the two ends
        // we do have. Raw keys are the last resort, not a translation table.
        if let o = origin_label ?? origin_key, let d = dest_label ?? dest_key {
            return "\(o) → \(d)"
        }
        return "Unmatched route"
    }

    /// Sort key for date+time. `start_utc` is fixed-width ISO, so comparing it
    /// as a string IS chronological; the fallback orders identically except
    /// inside the DST fall-back hour.
    var chronoKey: String {
        start_utc ?? String(format: "%@T%04d", local_date, local_mod)
    }

    var deltaMin: Int? {
        guard let d = duration_sec, let p = predicted_sec else { return nil }
        return Int((Double(d - p) / 60).rounded())
    }

    /// The web list tints exactly this row and it is the one worth acting on.
    var needsVerdict: Bool { needs_review == 1 && verdict == "auto" }

    /// Which filter chip owns this row. Derived from `verdict` alone, so the
    /// three chips partition the list: no trip can hide from all of them.
    var bucket: TripFilter {
        switch verdict {
        case "confirmed": .confirmed
        case "rejected": .rejected
        default: .pending
        }
    }
}

enum TripFilter: String, CaseIterable, Identifiable {
    case pending, confirmed, rejected
    var id: String { rawValue }
    var label: String {
        switch self {
        case .pending: "Pending"
        case .confirmed: "Confirmed"
        case .rejected: "Rejected"
        }
    }
}

enum TripSort: String, CaseIterable, Identifiable {
    case date, departure, route, actual, predicted, delta
    var id: String { rawValue }
    var label: String {
        switch self {
        case .date: "Date"
        case .departure: "Departure time"
        case .route: "Route"
        case .actual: "Actual"
        case .predicted: "Predicted"
        case .delta: "Δ vs predicted"
        }
    }
}

/// One ordering rule for every column, matching `trips.js` on the web: a
/// missing value is UNKNOWN, not small, so it sinks in BOTH directions rather
/// than flooding the top of every ascending sort; ties fall back to newest
/// first so re-sorting a coarse key can't reshuffle them differently each time.
private func tripOrder<K: Comparable>(_ a: TripRow, _ b: TripRow, _ asc: Bool,
                                      _ key: (TripRow) -> K?) -> Bool {
    let ka = key(a), kb = key(b)
    if let x = ka, let y = kb {
        if x != y { return asc ? x < y : x > y }
    } else if ka == nil, kb != nil {
        return false
    } else if ka != nil, kb == nil {
        return true
    }
    return a.chronoKey > b.chronoKey
}

/// Review and approve captured trips — the native twin of the web /trips
/// page. The server is the judge of what needs review; the app just renders
/// its queue and posts verdicts. Sort and filter state are the same two
/// choices the web page persists, so the surfaces read the same way.
struct TripsView: View {
    @Environment(\.colorScheme) private var scheme
    @State private var trips: [TripRow] = []
    @State private var error: String?
    @State private var busy: Set<Int> = []

    @AppStorage("trips.sort") private var sortRaw = TripSort.date.rawValue
    @AppStorage("trips.sortAscending") private var sortAscending = false
    /// Rejected starts off: those trips are already out of every average, so
    /// their only job is being findable again on demand.
    @AppStorage("trips.filters") private var filtersRaw = "pending,confirmed"

    private var sort: TripSort { TripSort(rawValue: sortRaw) ?? .date }

    private var filters: Set<TripFilter> {
        Set(filtersRaw.split(separator: ",").compactMap { TripFilter(rawValue: String($0)) })
    }

    private var kept: [TripRow] { trips.filter { filters.contains($0.bucket) } }

    private var visible: [TripRow] {
        let rows = kept, asc = sortAscending
        switch sort {
        case .date:      return rows.sorted { a, b in tripOrder(a, b, asc) { $0.chronoKey } }
        case .departure: return rows.sorted { a, b in tripOrder(a, b, asc) { $0.local_mod } }
        case .route:     return rows.sorted { a, b in tripOrder(a, b, asc) { $0.routeLabel.lowercased() } }
        case .actual:    return rows.sorted { a, b in tripOrder(a, b, asc) { $0.duration_sec } }
        case .predicted: return rows.sorted { a, b in tripOrder(a, b, asc) { $0.predicted_sec } }
        case .delta:     return rows.sorted { a, b in tripOrder(a, b, asc) { $0.deltaMin } }
        }
    }

    private var countLabel: String {
        guard !trips.isEmpty else { return "" }
        return kept.count == trips.count
            ? "\(trips.count) trip\(trips.count == 1 ? "" : "s")"
            : "\(kept.count) of \(trips.count) trips"
    }

    var body: some View {
        let t = Theme.current(scheme)
        let rows = visible
        List {
            if let error {
                Section { Text(error).foregroundStyle(.secondary) }
                    .listRowBackground(t.raised)
            }
            Section {
                if trips.isEmpty && error == nil {
                    Text("No trips captured yet").foregroundStyle(.secondary)
                        .listRowBackground(t.raised)
                } else if rows.isEmpty && !trips.isEmpty {
                    Text("Every trip is filtered out — turn a chip back on.")
                        .foregroundStyle(.secondary)
                        .listRowBackground(t.raised)
                }
                ForEach(rows) { trip in
                    row(t, trip)
                        // The same tint the web list paints on a flagged row.
                        .listRowBackground(trip.needsVerdict ? t.badTint : t.raised)
                }
            }
        }
        .scrollContentBackground(.hidden)
        .background(t.paper)
        .navigationTitle("Trips")
        // Inline, because the pinned chip bar sits where a large title would
        // expand: leaving it large costs a band of empty space AND drops the
        // title, which is worse than a small one.
        .navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .top) { chipBar(t) }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Picker("Sort by", selection: $sortRaw) {
                        ForEach(TripSort.allCases) { Text($0.label).tag($0.rawValue) }
                    }
                    .pickerStyle(.inline)
                    Picker("Order", selection: $sortAscending) {
                        Text("Descending").tag(false)
                        Text("Ascending").tag(true)
                    }
                    .pickerStyle(.inline)
                } label: {
                    Label("Sort", systemImage: "arrow.up.arrow.down")
                }
            }
        }
        .task { await load() }
        .refreshable { await load() }
    }

    /// Pinned above the list rather than scrolling with it: the chips are how
    /// you find a trip, so they have to stay reachable while you scroll.
    private func chipBar(_ t: Theme) -> some View {
        HStack(spacing: 6) {
            ForEach(TripFilter.allCases) { f in
                let on = filters.contains(f)
                Button { toggle(f) } label: {
                    Text(f.label)
                        .font(.caption2.weight(.semibold))
                        .padding(.horizontal, 10).padding(.vertical, 5)
                        .foregroundStyle(on ? Color.white : t.inkSoft)
                        .background(on ? t.accent : Color.clear, in: Capsule())
                        .overlay(Capsule().strokeBorder(on ? Color.clear : t.rule))
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(on ? [.isSelected] : [])
            }
            Spacer(minLength: 4)
            Text(countLabel).font(.caption2).foregroundStyle(t.inkFaint).lineLimit(1)
        }
        .padding(.horizontal, 16).padding(.vertical, 8)
        .background(t.paper)
        .overlay(alignment: .bottom) { Rectangle().fill(t.rule).frame(height: 0.5) }
    }

    private func toggle(_ f: TripFilter) {
        var next = filters
        if next.contains(f) { next.remove(f) } else { next.insert(f) }
        // Written back in canonical order so the stored string stays stable.
        filtersRaw = TripFilter.allCases.filter(next.contains).map(\.rawValue).joined(separator: ",")
    }

    private func row(_ t: Theme, _ trip: TripRow) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(trip.routeLabel).font(.subheadline.weight(.semibold))
                Spacer()
                statusChip(trip)
            }
            HStack(spacing: 6) {
                Text("\(trip.local_date) · \(Fmt.hhmm(trip.local_mod))")
                if let d = trip.duration_sec {
                    Text("· \(Fmt.min(d))")
                    if let delta = trip.deltaMin {
                        Text(delta > 0 ? "(+\(delta) vs predicted)" : delta < 0 ? "(\(delta) vs predicted)" : "(as predicted)")
                    }
                }
            }
            .font(.caption).foregroundStyle(.secondary)
            if let reason = trip.review_reason, trip.needs_review == 1 {
                Text(reason).font(.caption).foregroundStyle(.orange)
            }
            if trip.needsVerdict {
                HStack(spacing: 10) {
                    Button("Confirm") { Task { await verdict(trip, "confirmed") } }
                        .buttonStyle(.borderedProminent).tint(.green)
                    Button("Reject") { Task { await verdict(trip, "rejected") } }
                        .buttonStyle(.bordered).tint(.red)
                }
                .font(.footnote)
                .disabled(busy.contains(trip.id))
                .padding(.top, 2)
            }
        }
        .padding(.vertical, 2)
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            // Same gate the web row uses: a verdict only means something for a
            // trip that could enter an average. 'non_commute' cannot, so
            // offering Confirm here would be a button that changes nothing.
            if (trip.verdict == "auto" || trip.needs_review == 1),
               trip.status == "complete" || trip.status == "abandoned" {
                Button("Confirm") { Task { await verdict(trip, "confirmed") } }.tint(.green)
                Button("Reject") { Task { await verdict(trip, "rejected") } }.tint(.red)
            }
        }
    }

    private func statusChip(_ trip: TripRow) -> some View {
        let (label, color): (String, Color) = switch (trip.status, trip.verdict) {
        case (_, "confirmed"): ("confirmed", .green)
        case (_, "rejected"): ("rejected", .red)
        case ("open", _): ("open", .orange)
        case ("abandoned", _): ("abandoned", .gray)
        // Not awaiting a decision — the server already decided this drive is
        // not one of the tracked corridors. Falling through to "auto" would
        // read as "review me" and send you looking for a button to press.
        case ("non_commute", _): ("not a commute", .gray)
        default: ("auto", .secondary)
        }
        return Text(label)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(color.opacity(0.18), in: Capsule())
            .foregroundStyle(color)
    }

    private func load() async {
        do {
            let (data, code) = try await ServerClient.raw("/api/v1/trips?limit=60")
            guard code == 200 else { throw ServerClient.ClientError.http(code) }
            struct Payload: Codable { let trips: [TripRow] }
            trips = try JSONDecoder().decode(Payload.self, from: data).trips
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func verdict(_ trip: TripRow, _ verdict: String) async {
        busy.insert(trip.id)
        defer { busy.remove(trip.id) }
        let ok = await ServerClient.postVerdict(tripId: trip.id, verdict: verdict)
        if ok { await load() } else { error = "Couldn't save verdict — try again." }
    }
}

