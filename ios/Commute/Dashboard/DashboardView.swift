import SwiftUI
import Charts
import WidgetKit

/// Fully native dashboard — the four-zone information architecture, same as
/// the web app: NOW (is today weird) → PLANNING (what should I do) →
/// PATTERNS (why is it like this) → TRUST (can I believe you).
///
/// Direction scopes NOW and PATTERNS only. PLANNING is deliberately
/// direction-INDEPENDENT — it shows both half-days at once — which is what
/// makes a zone-local switcher honest rather than a global mode the user has
/// to remember. One /api/v1/dashboard request renders everything.
struct DashboardView: View {
    @EnvironmentObject var state: AppState
    @Environment(\.colorScheme) private var scheme
    @Environment(\.horizontalSizeClass) private var hSize
    @State private var payload: DashboardPayload?
    @State private var error: String?
    /// ONE piece of direction state. The NOW and PATTERNS eyebrows each render
    /// an instance of the same picker bound to it, so the two can never
    /// disagree. Default is clock-based: before noon you are going to work.
    @State private var dir: String = Calendar.current.component(.hour, from: Date()) < 12
        ? "office" : "home"

    /// The corridor the direction switcher currently points at, as the SERVER
    /// names it. Places are user-defined, so their slugs are too — there is no
    /// literal to fall back to, and a payload without `primary` gets an
    /// empty-state card rather than a guess at someone else's topology.
    private func route(_ p: DashboardPayload, _ heading: String) -> PrimaryRoute? {
        (p.primary?[heading] ?? nil)
    }
    private func current(_ p: DashboardPayload) -> PrimaryRoute? {
        route(p, dir == "office" ? "outbound" : "inbound")
    }
    private var t: Theme { Theme.current(scheme) }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 14) {
                    if let p = payload {
                        if hSize == .regular { regularLayout(p) } else { compactLayout(p) }
                    } else if let error {
                        Card {
                            Label(error, systemImage: "wifi.exclamationmark")
                                .foregroundStyle(.orange)
                            Text("Check the bearer token in Settings.")
                                .font(.footnote).foregroundStyle(t.inkFaint)
                        }
                    } else {
                        ProgressView().frame(maxWidth: .infinity, minHeight: 300)
                    }
                }
                .padding(.horizontal, 14)
                // One haptic per flip regardless of which switcher moved.
                .sensoryFeedback(.selection, trigger: dir)
            }
            .background(t.paper)
            .navigationTitle("Commute")
            .refreshable { await load() }
            .task { if payload == nil { await load() } }
        }
    }

    // ── layouts ───────────────────────────────────────────────────────────
    // Both branches consume the SAME zone builders, so compact and regular
    // cannot drift apart as cards are added.

    @ViewBuilder
    private func compactLayout(_ p: DashboardPayload) -> some View {
        nowZone(p)
        planningZone(p)
        patternsZone(p)
        trustZone(p)
    }

    /// 2×2 grid of whole zones — never a zone stitched across two columns,
    /// which is what the old interleaved layout did (Now/Hero left,
    /// Map/charts right) and why the page read as nine unrelated cards.
    @ViewBuilder
    private func regularLayout(_ p: DashboardPayload) -> some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(spacing: 14) { nowZone(p) }.frame(maxWidth: .infinity)
            VStack(spacing: 14) { planningZone(p) }.frame(maxWidth: .infinity)
        }
        HStack(alignment: .top, spacing: 14) {
            VStack(spacing: 14) { patternsZone(p) }.frame(maxWidth: .infinity)
            VStack(spacing: 14) { trustZone(p) }.frame(maxWidth: .infinity)
        }
    }

    // ── zones ─────────────────────────────────────────────────────────────

    @ViewBuilder
    private func nowZone(_ p: DashboardPayload) -> some View {
        ZoneEyebrow("Now") { directionSwitcher(p) }
        if let r = current(p) {
            NowCard(sample: p.now[r.slug] ?? nil, alts: p.alts?[r.slug] ?? [],
                    routeLabel: r.label)
                .animation(.snappy, value: dir)
            MapCard(slug: r.slug, destinationName: r.dest_label ?? "the destination",
                    reliability: p.reliability?[r.slug])
                .animation(.snappy, value: dir)
        } else {
            Card {
                Text("No corridor for this direction yet.")
                    .font(.footnote).foregroundStyle(t.inkFaint)
                Text("Add a home and an office under Places on the server's settings page.")
                    .font(.caption).foregroundStyle(t.inkFaint)
            }
        }
    }

    @ViewBuilder
    private func planningZone(_ p: DashboardPayload) -> some View {
        ZoneEyebrow("Planning")
        HeroPair(headline: p.headline)
        WhenToLeaveCard(outbound: route(p, "outbound").flatMap { p.routes[$0.slug] },
                        inbound: route(p, "inbound").flatMap { p.routes[$0.slug] },
                        outboundName: route(p, "outbound")?.dest_label,
                        inboundName: route(p, "inbound")?.dest_label,
                        sideBySide: hSize == .regular)
        PlanCard(plan: p.plan)
    }

    @ViewBuilder
    private func patternsZone(_ p: DashboardPayload) -> some View {
        ZoneEyebrow("Patterns") { directionSwitcher(p) }
        if let rs = current(p).flatMap({ p.routes[$0.slug] }) {
            // The heatmap's columns ARE weekdays, so the old "By weekday" card
            HeatmapCard(cells: rs.heatmap)
                .animation(.snappy, value: dir)
        } else {
            Card {
                Text("No pattern data for this direction yet.")
                    .font(.footnote).foregroundStyle(t.inkFaint)
            }
        }
    }

    @ViewBuilder
    private func trustZone(_ p: DashboardPayload) -> some View {
        ZoneEyebrow("Trust")
        DamageCard(headline: p.headline, cost: p.cost)
        ModelCheckCard(bias: p.bias)
        collectionHealth(p)
    }

    // ── the switcher (two instances, one state) ───────────────────────────

    /// Each segment names where you'd be GOING. A full "A → B" pair is the
    /// honest label but it does not survive a segmented control at 260 pt once
    /// places carry real names, and the destination alone is unambiguous —
    /// the two segments are the same trip reversed.
    private func directionSwitcher(_ p: DashboardPayload) -> some View {
        Picker("Direction", selection: $dir) {
            Text(route(p, "outbound")?.dest_label ?? "Out").tag("office")
            Text(route(p, "inbound")?.dest_label ?? "Back").tag("home")
        }
        .pickerStyle(.segmented)
        .labelsHidden()
        .frame(maxWidth: 260)
        .accessibilityLabel("Direction")
    }

    // ── collection health (the named fear: did polling quietly stop?) ─────

    @ViewBuilder
    private func collectionHealth(_ p: DashboardPayload) -> some View {
        let samples = liveSampleCount(p)
        let freshest = p.now.values.compactMap { $0?.ts_utc }
            .compactMap { Fmt.minutesAgo($0) }.min()
        let stale = isStale(freshest)
        HStack(spacing: 6) {
            Image(systemName: stale ? "exclamationmark.triangle.fill"
                                    : "dot.radiowaves.left.and.right")
            Text(healthLine(samples: samples, freshest: freshest))
            Spacer(minLength: 0)
        }
        .font(.caption)
        .foregroundStyle(stale ? t.bad : t.inkFaint)
        .padding(.horizontal, 4)
        .padding(.bottom, 10)
    }

    private func healthLine(samples: Int, freshest: Int?) -> String {
        guard let freshest else {
            return samples == 0 ? "No live samples collected yet"
                                : "\(samples) samples · no live reading yet"
        }
        return "\(samples) samples · newest \(Fmt.ago(minutes: freshest))"
    }

    /// Polls only run weekdays 7 AM–7 PM, so an overnight or weekend gap is
    /// expected, not a fault. Only alarm inside the window, where a >45-min
    /// gap really does mean the ~10-min poll stopped.
    private func isStale(_ minutesAgo: Int?) -> Bool {
        let cal = Calendar.current
        let weekday = cal.component(.weekday, from: Date())   // 1 = Sunday
        let hour = cal.component(.hour, from: Date())
        let inWindow = (2...6).contains(weekday) && (7..<19).contains(hour)
        guard inWindow else { return false }
        guard let minutesAgo else { return true }
        return minutesAgo > 45
    }

    /// Live samples in the weekday 7–18 window, both directions. Thin buckets
    /// fall back to the historical profile and report their live count
    /// separately, so both shapes have to be summed or the total undercounts.
    private func liveSampleCount(_ p: DashboardPayload) -> Int {
        p.routes.values.reduce(0) { acc, rs in
            acc + rs.hourly.reduce(0) { $0 + ($1.source == "live" ? $1.n : ($1.live_n ?? 0)) }
        }
    }

    private func load() async {
        do {
            let (data, code) = try await ServerClient.raw("/api/v1/dashboard")
            guard code == 200 else {
                error = code == 401 ? "Token rejected (401)" : "Server error \(code)"
                return
            }
            payload = try JSONDecoder().decode(DashboardPayload.self, from: data)
            error = nil
            WidgetCenter.shared.reloadAllTimelines()
        } catch {
            self.error = "Unreachable: \(error.localizedDescription)"
        }
    }
}

// ── zone eyebrow ──────────────────────────────────────────────────────────

/// Zone headers are eyebrows, not titles: the serif treatment belongs to
/// cards, and a second serif tier would compete with the numbers that matter.
struct ZoneEyebrow<Trailing: View>: View {
    @Environment(\.colorScheme) private var scheme
    private let title: String
    private let trailing: Trailing

    init(_ title: String, @ViewBuilder trailing: () -> Trailing) {
        self.title = title
        self.trailing = trailing()
    }

    var body: some View {
        let t = Theme.current(scheme)
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 12) {
                Text(title.uppercased())
                    .font(.caption2.weight(.semibold))
                    .kerning(1.6)
                    .foregroundStyle(t.inkFaint)
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 8)
                trailing
            }
            Rectangle().fill(t.rule).frame(height: 1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 4)
    }
}

extension ZoneEyebrow where Trailing == EmptyView {
    init(_ title: String) { self.init(title, trailing: { EmptyView() }) }
}

// ── Now ───────────────────────────────────────────────────────────────────

/// The live number, its delta, its freshness, and the four rows that used to
/// live in a separate "Route right now" card — which meant the same live
/// figure was printed twice on one screen.
struct NowCard: View {
    @Environment(\.colorScheme) private var scheme
    let sample: NowSample?
    /// Every corridor the map draws, primary included. The headline is one
    /// corridor's story; this is the choice between them.
    let alts: [AltLive]
    /// The route's own label, straight from the payload — this card used to
    /// spell out one deployment's office names from the direction alone.
    let routeLabel: String

    /// One corridor is not a comparison — the headline already printed it. The
    /// second clause covers the case where the primary has no live reading but
    /// an alternate does: the numbers exist, so they should be on screen.
    private var showsCorridors: Bool {
        alts.count > 1 || (sample?.travel_sec == nil && !alts.isEmpty)
    }
    private var primarySec: Int? { alts.first { $0.alt_index == 0 }?.travel_sec }

    var body: some View {
        let t = Theme.current(scheme)
        Card {
            if let s = sample, let live = s.travel_sec {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text(Fmt.bareMin(live))
                        .font(.system(size: 40, weight: .bold))
                    Text("min").font(.headline).foregroundStyle(t.inkSoft)
                    deltaChip(live: live, ref: s.historic_sec ?? s.free_flow_sec, t: t)
                    Spacer()
                }
                HStack(spacing: 6) {
                    Text(routeLabel)
                    if let ts = s.ts_utc, let m = Fmt.minutesAgo(ts) {
                        Text("·")
                        Text("read \(Fmt.ago(minutes: m))")
                    }
                }
                .font(.footnote).foregroundStyle(t.inkFaint)
                Divider()
                Grid(alignment: .leading, horizontalSpacing: 20, verticalSpacing: 4) {
                    detailRow("Live", s.travel_sec, t: t, emphasis: true)
                    detailRow("Typical", s.historic_sec, t: t)
                    detailRow("Free-flow", s.free_flow_sec, t: t)
                    delayRow(s.delay_sec, t: t)
                }
                .font(.footnote)
            } else {
                Text("No live sample yet — polls run weekdays 7 AM–7 PM")
                    .font(.footnote).foregroundStyle(t.inkFaint)
            }
            if showsCorridors { corridors(t) }
        }
    }

    // ── the corridors, side by side ───────────────────────────────────────
    // Same colors, same order, same numbers as the pills on the map above:
    // the row is how you read the map, not a second opinion about it.

    @ViewBuilder
    private func corridors(_ t: Theme) -> some View {
        Divider()
        Text("every corridor right now · colors match the map")
            .font(.caption).foregroundStyle(t.inkFaint)
        ForEach(alts) { a in
            HStack(spacing: 8) {
                Capsule().fill(CorridorStyle.swatch(a.alt_index))
                    .frame(width: 14, height: 3)
                Text(CorridorStyle.name(a.alt_index))
                    .font(.footnote.weight(a.alt_index == 0 ? .semibold : .regular))
                if let m = a.length_meters {
                    Text("~\(Double(m) / 1609.34, specifier: "%.1f") mi")
                        .font(.caption).foregroundStyle(t.inkFaint)
                }
                Spacer(minLength: 4)
                if let d = delta(a) { deltaText(d, t) }
                Text(Fmt.min(a.travel_sec))
                    .font(.footnote.weight(.semibold)).monospacedDigit()
            }
            // A reading older than the poll cycle is still true, just not
            // "now" — dimmed rather than hidden, since it's the only number
            // that corridor has.
            .opacity(a.stale == true ? 0.55 : 1)
            .accessibilityElement(children: .combine)
        }
    }

    /// Minutes this corridor costs against the primary. Nil for the primary
    /// itself (the baseline can't differ from itself) and when there is no
    /// baseline to measure against.
    private func delta(_ a: AltLive) -> Int? {
        guard a.alt_index != 0, let base = primarySec else { return nil }
        // Round each side to minutes BEFORE subtracting — the same order as the
        // web's renderAlternates, so the two surfaces can't print a different
        // gap for identical data. Subtracting first also lets the gap contradict
        // the figures beside it: 1470s and 1500s both display "25 min", but the
        // raw 30s difference rounds to "+1".
        let m = { (sec: Int) in Int((Double(sec) / 60).rounded()) }
        return m(a.travel_sec) - m(base)
    }

    @ViewBuilder
    private func deltaText(_ dm: Int, _ t: Theme) -> some View {
        // Only "faster than the corridor you're being sent down" is worth
        // color — that's the one that changes what the reader does.
        // U+2212 MINUS, not the ASCII hyphen Int interpolation would give —
        // the web prints the same glyph, and it aligns with the digits.
        Text(dm > 0 ? "+\(dm)" : dm < 0 ? "−\(abs(dm))" : "even")
            .font(.caption.weight(.semibold)).monospacedDigit()
            .foregroundStyle(dm < -2 ? t.good : t.inkFaint)
    }

    @ViewBuilder
    private func detailRow(_ label: String, _ sec: Int?, t: Theme,
                           emphasis: Bool = false) -> some View {
        if let sec {
            GridRow {
                Text(label).foregroundStyle(t.inkFaint)
                Text(Fmt.min(sec))
                    .fontWeight(emphasis ? .bold : .semibold)
                    .monospacedDigit()
            }
        }
    }

    @ViewBuilder
    private func delayRow(_ sec: Int?, t: Theme) -> some View {
        if let sec {
            let m = Int((Double(sec) / 60).rounded())
            GridRow {
                Text("Delay").foregroundStyle(t.inkFaint)
                Text(m > 0 ? "+\(m) min" : "none")
                    .fontWeight(.semibold).monospacedDigit()
                    .foregroundStyle(m > 2 ? t.bad : t.ink)
            }
        }
    }

    @ViewBuilder
    private func deltaChip(live: Int, ref: Int?, t: Theme) -> some View {
        if let ref {
            let dm = Int((Double(live - ref) / 60).rounded())
            let text = dm > 2 ? "+\(dm) min slower than usual"
                     : dm < -2 ? "\(-dm) min faster than usual" : "about usual"
            let bad = dm > 2
            let good = dm < -2
            Text(text)
                .font(.caption.weight(.semibold))
                .padding(.horizontal, 9).padding(.vertical, 3)
                .background(bad ? t.badTint : good ? t.goodTint : t.rule.opacity(0.4),
                            in: Capsule())
                .foregroundStyle(bad ? t.bad : good ? t.good : t.inkSoft)
        }
    }
}

// ── Hero pair with dotplots ───────────────────────────────────────────────

struct HeroPair: View {
    @Environment(\.colorScheme) private var scheme
    let headline: Headline

    var body: some View {
        let t = Theme.current(scheme)
        let hasDots = (headline.am_out?.dots?.count ?? 0) >= 2
            || (headline.pm_in?.dots?.count ?? 0) >= 2
        // 2-up: two sparse full-width cards wasted half the screen. The pair
        // shares a row at every width; numerals scale down before clipping.
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 14) {
                // Labels are the corridors' own, uppercased for the eyebrow —
                // the fallbacks say which half-day it is without naming places
                // this deployment may not have.
                hero(label: (headline.labels?.am_out ?? "Morning out").uppercased(),
                     sub: "leave 8:00 AM", b: headline.am_out)
                hero(label: (headline.labels?.pm_in ?? "Evening back").uppercased(),
                     sub: "leave 5:00 PM", b: headline.pm_in)
            }
            // ONE caption for both dotplots — printed under each card it was
            // four lines of identical prose in the most valuable space here.
            if hasDots {
                Text("20 dots · each is a 1-in-20 chance · count from the right for your risk of a bad day")
                    .font(.caption2).foregroundStyle(t.inkFaint)
                    .padding(.horizontal, 4)
            }
        }
    }

    private func hero(label: String, sub: String, b: DepartBucket?) -> some View {
        let t = Theme.current(scheme)
        return Card {
            Text(label)
                .font(.caption2.weight(.semibold)).kerning(1.4)
                .foregroundStyle(t.inkFaint)
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(Fmt.bareMin(b?.avg_sec))
                    .font(.system(size: 52, weight: .bold, design: .serif))
                    .minimumScaleFactor(0.5).lineLimit(1)
                    .opacity((b?.n ?? 0) < 5 ? 0.55 : 1)
                Text("min").font(.title3.weight(.semibold)).foregroundStyle(t.inkSoft)
            }
            if let b {
                Text("\(sub) · p90 \(Fmt.min(b.p90_sec)) · n=\(b.n)"
                     + (b.source == "historical" ? " · typical" : ""))
                    .font(.caption2).foregroundStyle(t.inkFaint)
                    .lineLimit(2)
                if let dots = b.dots, dots.count >= 2 {
                    DotplotView(dots: dots, color: t.seriesPredicted)
                        .frame(height: 44)

                }
            }
        }
    }
}

/// Wilkinson-binned quantile dotplot (Kay et al., CHI 2016) as a Canvas.
struct DotplotView: View {
    let dots: [Int]
    let color: Color

    var body: some View {
        Canvas { ctx, size in
            guard let lo = dots.min(), let hi = dots.max(), hi > lo else { return }
            let bins = 16
            let r: CGFloat = 4.5
            var stack = [Int: Int]()
            for d in dots {
                let bin = Swift.min(bins - 1, Int(Double(d - lo) / Double(hi - lo) * Double(bins)))
                let level = stack[bin, default: 0]
                stack[bin] = level + 1
                let cx = (CGFloat(bin) + 0.5) / CGFloat(bins) * (size.width - 2 * r) + r
                let cy = size.height - r - 1 - CGFloat(level) * (2 * r + 1)
                ctx.fill(Path(ellipseIn: CGRect(x: cx - r, y: Swift.max(r, cy) - r,
                                                width: 2 * r, height: 2 * r)),
                         with: .color(color))
            }
        }
    }
}

// ── When to leave — two half-days, not one flat all-day line ──────────────

/// Two panels, each scoped to the half-day anyone actually plans in. The old
/// single 7 AM–6 PM chart spent two-thirds of its plot on midday hours nobody
/// commutes in, and only ever showed one direction. Showing both half-days is
/// what makes the PLANNING zone direction-independent.
struct WhenToLeaveCard: View {
    @Environment(\.colorScheme) private var scheme
    let outbound: RouteStats?   // primary outbound — mornings
    let inbound: RouteStats?    // primary inbound — evenings
    /// Where each half-day is headed, from the payload. Panels are narrow, so
    /// this is the destination alone rather than the full "A → B" label.
    var outboundName: String?
    var inboundName: String?
    let sideBySide: Bool

    /// The server only buckets hours 7–18, so these are the hours that can
    /// exist inside the 6:30–10:30 / 15:00–19:00 planning windows.
    private static let morningHours = 7...10
    private static let eveningHours = 15...18

    var body: some View {
        let t = Theme.current(scheme)
        let mPred = Self.slice(outbound?.hourly, Self.morningHours)
        let ePred = Self.slice(inbound?.hourly, Self.eveningHours)
        let mAct = Self.slice(outbound?.actual_hourly, Self.morningHours)
        let eAct = Self.slice(inbound?.actual_hourly, Self.eveningHours)
        // Shared y-scale: the two panels sit side by side in the same card, and
        // independent axes would invent differences that aren't in the data.
        let y = Self.yDomain(mPred + ePred + mAct + eAct)
        let mTitle = outboundName.map { "Morning · to \($0)" } ?? "Morning"
        let eTitle = inboundName.map { "Evening · to \($0)" } ?? "Evening"

        Card {
            Text("When to leave").font(.title3.bold()).fontDesign(.serif)
            Text("shaded = 10th–90th percentile · 8 of 10 trips fall inside")
                .font(.caption).foregroundStyle(t.inkFaint)
            if sideBySide {
                HStack(alignment: .top, spacing: 14) {
                    panel(mTitle, mPred, mAct,
                          x: 6.5...10.5, ticks: [7, 8, 9, 10], y: y, t: t)
                    panel(eTitle, ePred, eAct,
                          x: 14.5...18.5, ticks: [15, 16, 17, 18], y: y, t: t)
                }
            } else {
                panel(mTitle, mPred, mAct,
                      x: 6.5...10.5, ticks: [7, 8, 9, 10], y: y, t: t)
                Divider()
                panel(eTitle, ePred, eAct,
                      x: 14.5...18.5, ticks: [15, 16, 17, 18], y: y, t: t)
            }
        }
    }

    @ViewBuilder
    private func panel(_ title: String, _ pred: [Bucket], _ actual: [Bucket],
                       x: ClosedRange<Double>, ticks: [Int],
                       y: ClosedRange<Double>, t: Theme) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.caption.weight(.semibold)).foregroundStyle(t.inkSoft)
            if pred.isEmpty && actual.isEmpty {
                Text("No data yet.").font(.footnote).foregroundStyle(t.inkFaint)
                    .frame(maxWidth: .infinity, minHeight: 110)
            } else {
                // x is plotted as Double, not Int: the panel domains have half-
                // hour padding (6.5…10.5), and a ClosedRange<Double> domain
                // against Int-plotted data is a type mismatch Charts silently
                // drops — you'd get an auto-fitted axis instead of the window.
                Chart {
                    ForEach(pred, id: \.hour) { b in
                        if let h = b.hour, let p10 = b.p10_sec, let p90 = b.p90_sec {
                            AreaMark(x: .value("Hour", Double(h)),
                                     yStart: .value("p10", Double(p10) / 60),
                                     yEnd: .value("p90", Double(p90) / 60))
                                .foregroundStyle(t.seriesPredicted.opacity(0.14))
                        }
                        if let h = b.hour, let p50 = b.p50_sec {
                            LineMark(x: .value("Hour", Double(h)),
                                     y: .value("median", Double(p50) / 60))
                                .foregroundStyle(t.seriesPredicted)
                                .lineStyle(StrokeStyle(lineWidth: 2, lineJoin: .round))
                        }
                    }
                    ForEach(actual, id: \.hour) { b in
                        if let h = b.hour, let p50 = b.p50_sec {
                            PointMark(x: .value("Hour", Double(h)),
                                      y: .value("actual", Double(p50) / 60))
                                .foregroundStyle(t.seriesActual)
                                .symbolSize(70)
                        }
                    }
                }
                .chartXScale(domain: x)
                .chartYScale(domain: y)
                .chartXAxis {
                    AxisMarks(values: ticks.map(Double.init)) { v in
                        AxisGridLine()
                        AxisValueLabel {
                            if let h = v.as(Double.self) {
                                Text(Fmt.hourLabel(Int(h)))
                            }
                        }
                    }
                }
                .chartYAxisLabel("min")
                .aspectRatio(1.6, contentMode: .fit)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// n-gating identical to the old card: a bucket needs 3 readings and a
    /// median before it may draw anything.
    private static func slice(_ buckets: [Bucket]?, _ hours: ClosedRange<Int>) -> [Bucket] {
        (buckets ?? []).filter {
            $0.n >= 3 && $0.p50_sec != nil && ($0.hour.map(hours.contains) ?? false)
        }
    }

    private static func yDomain(_ buckets: [Bucket]) -> ClosedRange<Double> {
        let lows = buckets.compactMap { $0.p10_sec ?? $0.p50_sec }.map { Double($0) / 60 }
        let highs = buckets.compactMap { $0.p90_sec ?? $0.p50_sec }.map { Double($0) / 60 }
        let lo = (lows.min() ?? 0) - 2
        let hi = (highs.max() ?? 60) + 2
        let bottom = Swift.max(0, lo)
        return bottom...Swift.max(bottom + 5, hi)
    }
}

// ── Plan your week ────────────────────────────────────────────────────────

struct PlanCard: View {
    @Environment(\.colorScheme) private var scheme
    let plan: Plan

    var body: some View {
        let t = Theme.current(scheme)
        Card {
            Text("Plan your week").font(.title3.bold()).fontDesign(.serif)
            if let w = plan.window {
                Text("best \(plan.picks) days · leave home ≥ \(Fmt.hhmm(w.earliest_mod)) · leave office ≤ \(Fmt.hhmm(w.latest_mod))")
                    .font(.caption).foregroundStyle(t.inkFaint)
            }
            ForEach(plan.days) { d in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(Fmt.dowNames[d.dow]).font(.subheadline.weight(.semibold))
                            if d.pick {
                                Text("GO")
                                    .font(.caption2.bold())
                                    .padding(.horizontal, 7).padding(.vertical, 2)
                                    .background(t.good, in: Capsule())
                                    .foregroundStyle(.white)
                            }
                        }
                        Text("→ \(Fmt.hhmm(d.leave_home_mod))   ← \(Fmt.hhmm(d.leave_office_mod))")
                            .font(.caption).foregroundStyle(t.inkFaint)
                    }
                    Spacer()
                    Text("\(Fmt.bareMin(d.total_sec)) min")
                        .font(.subheadline.bold()).monospacedDigit()
                        .foregroundStyle(d.pick ? t.ink : t.inkFaint)
                }
                .opacity(d.pick ? 1 : 0.6)
                if d.dow != plan.days.last?.dow { Divider() }
            }
        }
    }
}

// ── Heatmap ───────────────────────────────────────────────────────────────

struct HeatmapCard: View {
    @Environment(\.colorScheme) private var scheme
    let cells: [Bucket]

    var body: some View {
        let t = Theme.current(scheme)
        // aggregate 30-min slots to hourly cells, mirroring the web
        var grid = [Int: [Int: [Int]]]()   // hour -> dow -> [p50]
        for c in cells where c.n > 0 {
            guard let d = c.dow, let s = c.slot30, let v = c.p50_sec else { continue }
            grid[s / 2, default: [:]][d, default: []].append(v)
        }
        let values = grid.values.flatMap { $0.values.map { $0.reduce(0, +) / $0.count } }
        let lo = values.min() ?? 0
        let hi = values.max() ?? 1

        return Card {
            Text("Departure heatmap").font(.title3.bold()).fontDesign(.serif)
            Text("green is faster, red is slower · minutes")
                .font(.caption).foregroundStyle(t.inkFaint)
            Grid(horizontalSpacing: 2, verticalSpacing: 2) {
                GridRow {
                    Text("")
                    ForEach(0..<5, id: \.self) { d in
                        Text(Fmt.dowShort[d])
                            .font(.caption2.weight(.semibold)).foregroundStyle(t.inkFaint)
                    }
                }
                ForEach(7...18, id: \.self) { hour in
                    GridRow {
                        Text(Fmt.hourLabel(hour))
                            .font(.caption2.monospacedDigit()).foregroundStyle(t.inkFaint)
                            .gridColumnAlignment(.trailing)
                        ForEach(0..<5, id: \.self) { d in
                            heatCell(hour: hour, dow: d, grid: grid, lo: lo, hi: hi, t: t)
                        }
                    }
                }
            }
            Text("Compare days by reading across one hour row — that is the hour you actually leave.")
                .font(.caption2).foregroundStyle(t.inkFaint)
        }
    }


    @ViewBuilder
    private func heatCell(hour: Int, dow: Int, grid: [Int: [Int: [Int]]],
                          lo: Int, hi: Int, t: Theme) -> some View {
        if let vals = grid[hour]?[dow], !vals.isEmpty {
            let v = vals.reduce(0, +) / vals.count
            let step = hi > lo ? Swift.min(4, (v - lo) * 5 / (hi - lo)) : 2
            Text("\(Int((Double(v) / 60).rounded()))")
                .font(.caption2.weight(.semibold)).monospacedDigit()
                .frame(maxWidth: .infinity, minHeight: 26)
                .background(t.heat[step], in: RoundedRectangle(cornerRadius: 4))
                .foregroundStyle(step == 1 || step == 2 ? Color(hex: 0x1A1B2E) : .white)
        } else {
            RoundedRectangle(cornerRadius: 4)
                .fill(t.rule.opacity(0.3))
                .frame(maxWidth: .infinity, minHeight: 26)
        }
    }
}


// ── The damage ────────────────────────────────────────────────────────────

struct DamageCard: View {
    @Environment(\.colorScheme) private var scheme
    let headline: Headline
    let cost: Cost

    var body: some View {
        let t = Theme.current(scheme)
        Card {
            Text("The damage").font(.title3.bold()).fontDesign(.serif)
            // Which two places this compares — both user-named, so the card
            // says so rather than assuming the reader knows.
            if let primary = headline.labels?.primary_place,
               let baseline = headline.labels?.baseline_place {
                Text("\(primary) vs \(baseline), measured the same way")
                    .font(.caption).foregroundStyle(t.inkFaint)
            }
            if let m = headline.am_multiplier, (headline.am_out?.n ?? 0) >= 5 {
                Text("\(m.ratio, specifier: "%.1f")×")
                    .font(.system(size: 56, weight: .bold))
                    .foregroundStyle(t.accent)
                Text("+\(Fmt.bareMin(m.delta_sec)) min each way, mornings")
                    .font(.subheadline.weight(.semibold)).foregroundStyle(t.inkSoft)
                if let pm = headline.pm_multiplier {
                    Text("evenings: \(pm.ratio, specifier: "%.1f")× (+\(Fmt.bareMin(pm.delta_sec)) min)")
                        .font(.footnote).foregroundStyle(t.inkFaint)
                }
            } else {
                Text("—×").font(.system(size: 56, weight: .bold)).foregroundStyle(t.inkFaint)
                Text("comparison unlocks at 5 samples per place")
                    .font(.footnote).foregroundStyle(t.inkFaint)
            }
            Divider()
            if let c = cost.new {
                Grid(alignment: .leading, horizontalSpacing: 20, verticalSpacing: 4) {
                    GridRow {
                        Text("Per day").foregroundStyle(t.inkFaint)
                        Text("\(c.day_min) min").bold().monospacedDigit()
                    }
                    GridRow {
                        Text("Per week (\(Int(cost.office_days_per_week ?? 3)) office days)")
                            .foregroundStyle(t.inkFaint)
                        Text("\(c.hrs_week, specifier: "%.1f") hrs").bold().monospacedDigit()
                    }
                    GridRow {
                        Text("Per year").foregroundStyle(t.inkFaint)
                        HStack(spacing: 6) {
                            Text("\(c.hrs_year) hrs").bold().monospacedDigit()
                            if let o = cost.old, c.hrs_year != o.hrs_year {
                                Text("+\(c.hrs_year - o.hrs_year)")
                                    .foregroundStyle(t.accentDeep).bold()
                            }
                        }
                    }
                    if let fuel = cost.fuel_year_new {
                        GridRow {
                            Text("Fuel / year").foregroundStyle(t.inkFaint)
                            Text("$\(fuel)").bold().monospacedDigit()
                        }
                    }
                }
                .font(.footnote)
            }
        }
    }
}

// ── Model check scatter ───────────────────────────────────────────────────

struct ModelCheckCard: View {
    @Environment(\.colorScheme) private var scheme
    let bias: Bias

    var body: some View {
        let t = Theme.current(scheme)
        Card {
            Text("Model check").font(.title3.bold()).fontDesign(.serif)
            if bias.n >= 5, let err = bias.mean_error_sec {
                Text("Across \(bias.n) real trips, the API runs \(Fmt.bareMin(abs(err))) min \(err > 0 ? "optimistic" : "pessimistic") on average.")
                    .font(.caption).foregroundStyle(t.inkFaint)
            } else {
                Text("Fills in as real trips are captured.")
                    .font(.caption).foregroundStyle(t.inkFaint)
            }
            if !bias.points.isEmpty {
                let maxV = Double(bias.points.map { max($0.predicted_sec, $0.actual_sec) }.max() ?? 3600) / 60 * 1.1
                Chart {
                    LineMark(x: .value("p", 0.0), y: .value("a", 0.0),
                             series: .value("s", "diag"))
                        .foregroundStyle(t.rule)
                    LineMark(x: .value("p", maxV), y: .value("a", maxV),
                             series: .value("s", "diag"))
                        .foregroundStyle(t.rule)
                    ForEach(bias.points) { p in
                        PointMark(x: .value("predicted", Double(p.predicted_sec) / 60),
                                  y: .value("actual", Double(p.actual_sec) / 60))
                            .foregroundStyle(p.actual_sec > p.predicted_sec ? t.bad : t.good)
                            .symbolSize(70)
                    }
                }
                .chartXAxisLabel("predicted (min)")
                .chartYAxisLabel("actual (min)")
                .aspectRatio(1.6, contentMode: .fit)
            }
        }
    }
}
