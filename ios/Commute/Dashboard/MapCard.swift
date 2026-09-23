import SwiftUI
import MapKit

/// Native traffic map — Apple's own congestion layer colors the basemap
/// (free, no TomTom tiles, no API key), and the server's corridor geometry
/// rides on top so you can see WHICH corridors the poller is timing.
/// Polylines carry identity, not traffic: blue = the primary (TomTom's current
/// pick), thin purple/pink = the alternates. Red is reserved for congestion —
/// Apple's own layer speaks it, so our lines only wear it on the primary's slow
/// sections. Each line wears its own live time at its midpoint and the trip's
/// two ends wear house/office badges, so the map answers "which one, and how
/// long" without a legend. Under the map, per-corridor reliability answers
/// "which one can I trust" — the buffer (p90 − p50) is the stat that matters
/// for planning, not the median.
struct MapCard: View {
    @Environment(\.colorScheme) private var scheme
    let slug: String
    /// Where this corridor ends, as the server labels it — the nav handoff
    /// names the destination out loud, and deriving it from the slug is how
    /// "Office" ended up on the screen of anyone whose office isn't called that.
    let destinationName: String
    let reliability: Reliability?
    @State private var destination: CLLocationCoordinate2D?
    @State private var showNavChooser = false
    /// Driven length per drawn line, in draw order (primary first). Used to key
    /// reliability rows to map colors BY LENGTH — never by alt_index, which
    /// flip-flops between corridors as their live ranking changes (the same
    /// reason route_reliability clusters by length server-side).
    @State private var lineLengths: [Int] = []
    /// nil = corridors loaded; otherwise the coordinator's failure message.
    @State private var mapError: String?
    @State private var mapReload = 0

    var body: some View {
        let t = Theme.current(scheme)
        Card {
            Text("The road").font(.title3.bold()).fontDesign(.serif)
            Text("live traffic · blue = your corridor · red = slow · time on each line")
                .font(.caption).foregroundStyle(t.inkFaint)
            TrafficMapView(slug: slug,
                           dark: scheme == .dark,
                           reloadToken: mapReload,
                           onDestination: { destination = $0 },
                           onLengths: { lineLengths = $0 },
                           onError: { mapError = $0 })
                .frame(height: 260)
                .clipShape(RoundedRectangle(cornerRadius: 9))
                .overlay(RoundedRectangle(cornerRadius: 9).stroke(t.rule, lineWidth: 1))
                .overlay(alignment: .bottomTrailing) { navigateButton(t) }
                .overlay { if let mapError { mapErrorOverlay(mapError, t) } }
                // A direction flip starts a fresh load — don't let the old
                // direction's failure sit over the new fetch.
                .onChange(of: slug) { mapError = nil }
            corridorRows(t)
        }
    }

    /// Same Label + wifi.exclamationmark treatment as DashboardView's error
    /// card, floated over the map so a failed fetch can't masquerade as a
    /// deliberately zoomed-out map.
    private func mapErrorOverlay(_ message: String, _ t: Theme) -> some View {
        VStack(spacing: 10) {
            Label(message, systemImage: "wifi.exclamationmark")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.orange)
            Button("Retry") {
                mapError = nil
                mapReload += 1
            }
            .font(.footnote.weight(.semibold))
            .buttonStyle(.bordered)
            .tint(t.accent)
        }
        .padding(14)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 9))
    }

    // ── hand off to a real nav app for turn-by-turn ───────────────────────
    // Commute keeps tracking in the background regardless — the geofences,
    // motion confirmation, and recorder don't care who owns the screen.

    private func navigateButton(_ t: Theme) -> some View {
        Button {
            showNavChooser = true
        } label: {
            // NOT "arrow.triangle.turn.up.right" — that name does not exist
            // (the triangle variants all require a .circle/.diamond suffix), and
            // Image(systemName:) draws a missing symbol as an EMPTY image, so
            // the button was a blank material disc for its whole life. The
            // circle here comes from .background below, so the plain arrow is
            // the one that doesn't double up.
            Image(systemName: "arrow.turn.up.right")
                .font(.system(size: 17, weight: .semibold))
                .frame(width: 44, height: 44)
                .background(.regularMaterial, in: Circle())
                .foregroundStyle(t.accent)
        }
        .padding(10)
        .disabled(destination == nil)
        .accessibilityLabel("Navigate to \(destinationName.lowercased())")
        .confirmationDialog("Navigate with", isPresented: $showNavChooser,
                            titleVisibility: .visible) {
            Button("Apple Maps") { openAppleMaps() }
            if canOpen("comgooglemaps://") {
                Button("Google Maps") {
                    open("comgooglemaps://?daddr=\(coords)&directionsmode=driving")
                }
            }
            if canOpen("waze://") {
                Button("Waze") { open("waze://?ll=\(coords)&navigate=yes") }
            }
            if canOpen("tomtomgo://") {
                Button("TomTom GO") {
                    open("tomtomgo://x-callback-url/navigate?destination=\(coords)")
                }
            }
        }
    }

    private var coords: String {
        guard let d = destination else { return "" }
        return "\(d.latitude),\(d.longitude)"
    }
    private func canOpen(_ scheme: String) -> Bool {
        URL(string: scheme).map { UIApplication.shared.canOpenURL($0) } ?? false
    }
    private func open(_ urlString: String) {
        if let url = URL(string: urlString) { UIApplication.shared.open(url) }
    }
    private func openAppleMaps() {
        guard let d = destination else { return }
        let item = MKMapItem(placemark: MKPlacemark(coordinate: d))
        item.name = destinationName
        item.openInMaps(launchOptions:
            [MKLaunchOptionsDirectionsModeKey: MKLaunchOptionsDirectionsModeDriving])
    }

    // ── reliability rows ──────────────────────────────────────────────────

    @ViewBuilder
    private func corridorRows(_ t: Theme) -> some View {
        if let rel = reliability, !rel.corridors.isEmpty {
            ForEach(Array(rel.corridors.enumerated()), id: \.offset) { i, c in
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            if let line = lineIndex(for: c) {
                                // Ringed for the same reason as the web legend:
                                // the deep purple is chosen for the MAP, and a
                                // 3pt bar of it needs an edge to read on dusk.
                                Capsule().fill(CorridorStyle.swatch(line))
                                    .frame(width: 14, height: 3)
                                    .overlay(Capsule().stroke(t.rule, lineWidth: 0.5))
                            }
                            Text("~\(c.mi, specifier: "%.1f") mi")
                                .font(.subheadline.weight(.semibold)).monospacedDigit()
                            Text("n=\(c.n)").font(.caption).foregroundStyle(t.inkFaint)
                            if rel.steadiest == i { steadiestChip(t) }
                        }
                        Text("p90 \(Fmt.min(c.p90_sec))"
                             + (c.buffer_sec.map { " · +\(Fmt.bareMin($0)) min buffer" } ?? ""))
                            .font(.caption).foregroundStyle(t.inkFaint)
                    }
                    Spacer()
                    Text(Fmt.min(c.p50_sec))
                        .font(.subheadline.bold()).monospacedDigit()
                }
                .opacity(c.n < 8 ? 0.6 : 1)
                if i < rel.corridors.count - 1 { Divider() }
            }
        } else {
            Text("Not enough live samples yet")
                .font(.footnote).foregroundStyle(t.inkFaint)
        }
    }

    /// Which drawn line is this corridor? Match on driven length — the map is
    /// ordered by alt_index and the reliability list by length cluster, so a
    /// naive index-to-index legend would mislabel corridors outright.
    private func lineIndex(for c: Reliability.Corridor) -> Int? {
        guard !lineLengths.isEmpty else { return nil }
        let target = c.mi * 1609.34
        return lineLengths.enumerated()
            .min(by: { abs(Double($0.element) - target) < abs(Double($1.element) - target) })?
            .offset
    }

    /// Same capsule recipe as TripsView's statusChip, green-tinted.
    private func steadiestChip(_ t: Theme) -> some View {
        Text("steadiest")
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(t.good.opacity(0.18), in: Capsule())
            .foregroundStyle(t.good)
    }
}

// ── MKMapView wrapper ─────────────────────────────────────────────────────

/// Mirror of /api/v1/route?alts=1 — only the fields the map needs. Each alt
/// payload's features are congestion-section slices of ONE LineString; we
/// concatenate them back into a single polyline per corridor because Apple's
/// traffic layer already paints congestion on the basemap.
private struct RouteAlts: Decodable {
    struct Payload: Decodable {
        struct Feature: Decodable {
            struct Props: Decodable { let mag: Int? }
            struct Geometry: Decodable { let coordinates: [[Double]] }
            let geometry: Geometry
            let properties: Props?
        }
        /// The live reading for THIS corridor. Every alternate has carried one
        /// all along; only the primary's was ever read, which is why comparing
        /// corridors meant guessing from line length.
        struct Summary: Decodable {
            let travel_sec: Int?
            let free_flow_sec: Int?
            let historic_sec: Int?
            let delay_sec: Int?
            let ts_utc: String?
        }
        let features: [Feature]
        let alt_index: Int?
        let length_meters: Int?
        let summary: Summary?
        let stale: Bool?
    }
    let alts: [Payload]
    let bbox: [Double]?   // [minLon, minLat, maxLon, maxLat] — union of alts
}

/// Corridor line colors, shared by the map renderer and the legend swatches so
/// a row and its line can never disagree. Must stay in lockstep with CORRIDOR
/// in `server/app/static/js/commute.js` — the same corridors, two screens.
///
/// Apple's traffic layer owns green→amber→RED underneath us, so red here means
/// congestion and nothing else. Corridor IDENTITY lives on the blue→purple→pink
/// arc that ramp never touches. Theme-invariant on purpose: all four clear 3:1
/// on the basemap and pass the all-pairs gate — worst ΔE 14.8 protan, 16.7
/// normal (Euclidean OKLab x100, CVD per Machado-Oliveira-Fernandes 2009 at
/// severity 1.0). The crimson pair they replaced was not a CVD failure; it
/// failed on contrast and on meaning.
enum CorridorStyle {
    /// Yours — the "selected route" blue every nav app uses.
    static let primary = UIColor(red: 0.145, green: 0.388, blue: 0.922, alpha: 1)   // #2563eb
    /// Congestion, and the only red we paint.
    static let slow = UIColor(red: 0.784, green: 0.153, blue: 0.110, alpha: 1)      // #c8271c
    static func alternate(_ i: Int) -> UIColor {
        i == 0 ? UIColor(red: 0.561, green: 0.110, blue: 0.541, alpha: 1)  // #8f1c8a purple
               : UIColor(red: 0.898, green: 0.333, blue: 0.604, alpha: 1)  // #e5559a pink
    }
    static func swatch(_ i: Int) -> Color { Color(line(i)) }

    /// Identity color keyed by the server's `alt_index` — the same key the line,
    /// its time pill, and its NOW-card row all resolve through, so none of the
    /// three can end up wearing another corridor's color. Never key off array
    /// position: a corridor with no live sample is absent, not null.
    ///
    /// No `dark:` parameter on purpose: corridor hues are theme-invariant (all
    /// four clear 3:1 on Apple's basemap, which never changes). Only `casing`
    /// follows the page ground.
    static func line(_ altIndex: Int) -> UIColor {
        altIndex == 0 ? primary : alternate(altIndex - 1)
    }

    /// The page ground, drawn under every corridor mark. It is what lets flat
    /// lines and pills sit ON a busy basemap instead of dissolving into it.
    static func casing(dark: Bool) -> UIColor {
        dark ? UIColor(red: 0.059, green: 0.063, blue: 0.125, alpha: 1) : .white
    }

    /// Corridors have no names — the color IS the name. This stays positional
    /// so a label can never claim a road the geometry didn't promise.
    static func name(_ altIndex: Int) -> String {
        altIndex == 0 ? "Primary" : "Alternate \(altIndex)"
    }
}

// ── map annotations ───────────────────────────────────────────────────────

/// One corridor's live time, riding on its own line. The number belongs next to
/// the line it describes: a legend under the map makes the reader hold three
/// colors in their head while looking somewhere else.
private final class CorridorLabel: NSObject, MKAnnotation {
    let coordinate: CLLocationCoordinate2D
    let altIndex: Int
    let text: String
    let stale: Bool

    init(coordinate: CLLocationCoordinate2D, altIndex: Int, text: String, stale: Bool) {
        self.coordinate = coordinate
        self.altIndex = altIndex
        self.text = text
        self.stale = stale
    }

    /// VoiceOver gets the color's meaning spelled out — the pill carries it as
    /// hue only, which is exactly the information a screen reader can't see.
    var spoken: String { "\(CorridorStyle.name(altIndex)) corridor, \(text)" }
}

/// A trip endpoint. `kind` is the server's, not ours: deriving home-vs-office
/// from the place key in each client is how the two surfaces drift apart.
private final class EndpointPin: NSObject, MKAnnotation {
    let coordinate: CLLocationCoordinate2D
    let kind: String        // "home" | "office"
    let title: String?      // the place's own label

    init(coordinate: CLLocationCoordinate2D, kind: String, title: String?) {
        self.coordinate = coordinate
        self.kind = kind
        self.title = title
    }
}

/// Annotation artwork. Both marks are pre-rendered images rather than hosted
/// views because MapKit's collision engine only arbitrates between
/// `MKAnnotationView`s that know their own size up front.
private enum MapGlyph {
    /// Filled pill, white bold text, cased like the polylines — the casing is
    /// what keeps it readable over roads, labels, and Apple's traffic ramp.
    static func pill(_ text: String, fill: UIColor, casing: UIColor) -> UIImage {
        let font = UIFont.systemFont(ofSize: 12, weight: .bold)
        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: UIColor.white]
        let text = text as NSString
        let inner = text.size(withAttributes: attrs)
        let pad = CGSize(width: 9, height: 4)
        let size = CGSize(width: ceil(inner.width) + pad.width * 2 + 3,
                          height: ceil(inner.height) + pad.height * 2 + 3)
        return UIGraphicsImageRenderer(size: size).image { _ in
            let body = CGRect(origin: .zero, size: size).insetBy(dx: 1.5, dy: 1.5)
            let shape = UIBezierPath(roundedRect: body, cornerRadius: body.height / 2)
            fill.setFill()
            shape.fill()
            casing.setStroke()
            shape.lineWidth = 3
            shape.stroke()
            text.draw(at: CGPoint(x: body.minX + pad.width, y: body.minY + pad.height),
                      withAttributes: attrs)
        }
    }

    /// Circular badge on the page ground, SF Symbol in ink — deliberately NOT a
    /// corridor color: the endpoints are shared by all three corridors, so
    /// tinting them would claim an identity they don't have.
    ///
    /// Every symbol name here is verified with UIImage(systemName:) against the
    /// SDK, not read off a chart. An unknown name draws an EMPTY image with no
    /// warning, which is how this app shipped a blank button for months.
    static func endpoint(_ symbol: String, ink: UIColor, ground: UIColor) -> UIImage {
        let d: CGFloat = 30
        let glyph = UIImage(systemName: symbol,
                            withConfiguration: UIImage.SymbolConfiguration(
                                pointSize: 14, weight: .semibold))?
            .withTintColor(ink, renderingMode: .alwaysOriginal)
        return UIGraphicsImageRenderer(size: CGSize(width: d, height: d)).image { _ in
            let circle = UIBezierPath(ovalIn: CGRect(x: 1.5, y: 1.5, width: d - 3, height: d - 3))
            ground.setFill()
            circle.fill()
            ink.withAlphaComponent(0.45).setStroke()
            circle.lineWidth = 1.5
            circle.stroke()
            if let glyph {
                glyph.draw(in: CGRect(x: (d - glyph.size.width) / 2,
                                      y: (d - glyph.size.height) / 2,
                                      width: glyph.size.width, height: glyph.size.height))
            }
        }
    }
}

private struct TrafficMapView: UIViewRepresentable {
    let slug: String
    var dark: Bool = true
    /// Bumped by MapCard's Retry button — any change forces a re-fetch even
    /// though the slug is unchanged.
    var reloadToken: Int = 0
    var onDestination: (CLLocationCoordinate2D) -> Void = { _ in }
    var onLengths: ([Int]) -> Void = { _ in }
    /// nil = loaded fine; otherwise a short user-facing message. Without this
    /// a failed fetch leaves the map at continent zoom with no overlays,
    /// which reads as a design choice rather than an error.
    var onError: (String?) -> Void = { _ in }

    func makeUIView(context: Context) -> MKMapView {
        let map = MKMapView()
        map.delegate = context.coordinator
        map.showsTraffic = true
        map.pointOfInterestFilter = .excludingAll
        map.isPitchEnabled = false
        context.coordinator.reloadToken = reloadToken
        context.coordinator.load(slug, into: map)
        return map
    }

    func updateUIView(_ map: MKMapView, context: Context) {
        // Re-fetch only on direction flips or an explicit retry; SwiftUI
        // re-renders are free.
        guard context.coordinator.slug != slug
            || context.coordinator.reloadToken != reloadToken else { return }
        context.coordinator.reloadToken = reloadToken
        context.coordinator.load(slug, into: map)
    }

    static func dismantleUIView(_ map: MKMapView, coordinator: Coordinator) {
        coordinator.cancel()
        map.delegate = nil
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(dark: dark, onDestination: onDestination,
                    onLengths: onLengths, onError: onError)
    }

    final class Coordinator: NSObject, MKMapViewDelegate {
        private(set) var slug: String?
        var reloadToken = 0
        private var task: Task<Void, Never>?
        private var hasFit = false
        /// Endpoints change when someone edits a place at /settings, not while
        /// the map is on screen — one fetch per map, reused across direction
        /// flips. The coordinator outlives those flips; the map view doesn't.
        private var config: ServerClient.Config?
        let dark: Bool
        let onDestination: (CLLocationCoordinate2D) -> Void
        let onLengths: ([Int]) -> Void
        let onError: (String?) -> Void

        init(dark: Bool,
             onDestination: @escaping (CLLocationCoordinate2D) -> Void,
             onLengths: @escaping ([Int]) -> Void,
             onError: @escaping (String?) -> Void) {
            self.dark = dark
            self.onDestination = onDestination
            self.onLengths = onLengths
            self.onError = onError
        }

        func cancel() { task?.cancel() }

        func load(_ slug: String, into map: MKMapView) {
            self.slug = slug
            task?.cancel()
            task = Task { @MainActor [weak map] in
                let fetched = try? await ServerClient.raw(
                    "/api/v1/route?route=\(slug)&alts=1")
                // A cancelled fetch also lands in the nil branch, so the
                // cancellation check must come first or a superseded load
                // would report "unreachable" over a perfectly good map.
                guard !Task.isCancelled, let map else { return }
                guard let (data, code) = fetched else {
                    onError("Couldn't reach the server")
                    return
                }
                guard code == 200 else {
                    onError("Server error \(code)")
                    return
                }
                guard let resp = try? JSONDecoder().decode(RouteAlts.self, from: data) else {
                    onError("Couldn't read route data")
                    return
                }
                guard !resp.alts.isEmpty else {
                    onError("No route corridors yet")
                    return
                }
                map.removeOverlays(map.overlays)
                map.removeAnnotations(map.annotations)
                // Alternates first so the primary draws on top of them.
                func points(_ f: RouteAlts.Payload.Feature) -> [CLLocationCoordinate2D] {
                    f.geometry.coordinates.compactMap { c in
                        c.count >= 2
                            ? CLLocationCoordinate2D(latitude: c[1], longitude: c[0]) : nil
                    }
                }

                // MKMapView draws overlays in add order, so every casing goes
                // down before any stroke — otherwise a later corridor's casing
                // would erase an earlier corridor's line.
                let ordered = resp.alts.sorted { ($0.alt_index ?? 0) < ($1.alt_index ?? 0) }
                var casings: [MKPolyline] = []
                var strokes: [MKPolyline] = []
                var labels: [CorridorLabel] = []

                for alt in ordered {
                    let idx = alt.alt_index ?? 0
                    let whole = alt.features.flatMap(points)
                    guard whole.count >= 2 else { continue }

                    // No reading, no pill — an empty corridor says "no data"
                    // far better than "0 min" does.
                    if let sec = alt.summary?.travel_sec, sec > 0,
                       let at = Self.midpoint(whole) {
                        labels.append(CorridorLabel(coordinate: at, altIndex: idx,
                                                    text: Fmt.min(sec),
                                                    stale: alt.stale ?? false))
                    }

                    let casing = MKPolyline(coordinates: whole, count: whole.count)
                    casing.title = "casing-\(idx)"
                    casings.append(casing)

                    if idx == 0 {
                        // Primary keeps its congestion sections so slow stretches
                        // can switch to the congestion red; the rest of the
                        // corridor stays one blue, so hue never has to mean two
                        // things at once.
                        for f in alt.features {
                            let seg = points(f)
                            guard seg.count >= 2 else { continue }
                            let line = MKPolyline(coordinates: seg, count: seg.count)
                            line.title = (f.properties?.mag ?? 0) >= 2 ? "slow" : "primary"
                            strokes.append(line)
                        }
                        if let end = whole.last { onDestination(end) }
                    } else {
                        let line = MKPolyline(coordinates: whole, count: whole.count)
                        line.title = "alt-\(idx - 1)"
                        strokes.append(line)
                    }
                }
                casings.forEach(map.addOverlay)
                strokes.forEach(map.addOverlay)
                map.addAnnotations(labels)
                onLengths(ordered.compactMap { $0.length_meters })
                onError(nil)
                fit(resp.bbox, map: map)
                await self.addEndpoints(slug, to: map)
            }
        }

        /// House and office pins, joined through the route's origin/dest. The
        /// coordinates are the server's — hardcoding them here is how the map
        /// ends up pointing at a place the geofences no longer watch.
        private func addEndpoints(_ slug: String, to map: MKMapView) async {
            if config == nil { config = try? await ServerClient.fetchConfig() }
            guard !Task.isCancelled, self.slug == slug,
                  let route = config?.routes?.first(where: { $0.slug == slug })
            else { return }
            for place in [route.origin, route.dest].compactMap({ $0 }) {
                guard let kind = place.kind else { continue }   // pre-`kind` server
                map.addAnnotation(EndpointPin(
                    coordinate: CLLocationCoordinate2D(latitude: place.lat,
                                                       longitude: place.lon),
                    kind: kind, title: place.label))
            }
        }

        /// The point at half the DRIVEN distance, not the middle array element.
        /// Vertex density tracks how twisty a stretch is — an interchange gets
        /// dozens, ten miles of highway gets two — so the middle index can sit
        /// miles off center. Distance is also what makes the three pills
        /// separate: corridors share both endpoints and only diverge in between.
        private static func midpoint(_ pts: [CLLocationCoordinate2D]) -> CLLocationCoordinate2D? {
            guard pts.count >= 2 else { return pts.first }
            let mp = pts.map(MKMapPoint.init)
            let legs = (1..<mp.count).map { mp[$0].distance(to: mp[$0 - 1]) }
            let half = legs.reduce(0, +) / 2
            guard half > 0 else { return pts.first }
            var walked = 0.0
            for (i, leg) in legs.enumerated() {
                guard walked + leg >= half else { walked += leg; continue }
                let f = (half - walked) / leg
                let a = mp[i], b = mp[i + 1]
                return MKMapPoint(x: a.x + (b.x - a.x) * f,
                                  y: a.y + (b.y - a.y) * f).coordinate
            }
            return pts.last
        }

        /// Frame the union bbox (all corridors at once) with breathing room.
        /// First load snaps; direction flips animate.
        private func fit(_ bbox: [Double]?, map: MKMapView) {
            var rect = MKMapRect.null
            if let b = bbox, b.count == 4 {
                let p1 = MKMapPoint(CLLocationCoordinate2D(latitude: b[1], longitude: b[0]))
                let p2 = MKMapPoint(CLLocationCoordinate2D(latitude: b[3], longitude: b[2]))
                rect = MKMapRect(x: Swift.min(p1.x, p2.x), y: Swift.min(p1.y, p2.y),
                                 width: abs(p1.x - p2.x), height: abs(p1.y - p2.y))
            } else {
                for o in map.overlays { rect = rect.union(o.boundingMapRect) }
            }
            guard !rect.isNull else { return }
            // Fitting before the view has been laid out computes against a zero
            // (or storyboard-default) size, which is why the default zoom was
            // wrong on first paint. Defer until there's real geometry.
            let apply = {
                map.setVisibleMapRect(
                    rect,
                    // extra bottom clears the Navigate overlay button
                    edgePadding: UIEdgeInsets(top: 28, left: 28, bottom: 44, right: 28),
                    animated: self.hasFit)
                self.hasFit = true
            }
            if map.bounds.width < 1 || map.bounds.height < 1 {
                DispatchQueue.main.async(execute: apply)
            } else {
                apply()
            }
        }

        func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
            guard let line = overlay as? MKPolyline else {
                return MKOverlayRenderer(overlay: overlay)
            }
            let r = MKPolylineRenderer(polyline: line)
            r.lineCap = .round
            r.lineJoin = .round
            let key = line.title ?? "primary"
            // Full opacity everywhere: a translucent thin line on a busy
            // basemap reads as road-network noise, not as data. Identity is
            // hue, hierarchy is width, separation is the casing.
            if key.hasPrefix("casing") {
                r.strokeColor = CorridorStyle.casing(dark: dark)
                r.lineWidth = key == "casing-0" ? 9 : 7
            } else if key == "primary" {
                r.strokeColor = CorridorStyle.primary
                r.lineWidth = 6
            } else if key == "slow" {
                r.strokeColor = CorridorStyle.slow
                r.lineWidth = 6
            } else {
                let i = Int(key.dropFirst("alt-".count)) ?? 0
                r.strokeColor = CorridorStyle.alternate(i)
                r.lineWidth = 4
            }
            return r
        }

        /// Collision is MapKit's job, and it only does it for views that opt in:
        /// `collisionMode` says "arbitrate me", `displayPriority` says who wins.
        /// The primary is `.required` — it is the corridor the reader is on, so
        /// it stays legible and the alternates hide under it rather than
        /// stacking into a smear where the three corridors converge.
        func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
            switch annotation {
            case let label as CorridorLabel:
                let v = mapView.dequeueReusableAnnotationView(withIdentifier: "corridor")
                    ?? MKAnnotationView(annotation: label, reuseIdentifier: "corridor")
                v.annotation = label
                v.image = MapGlyph.pill(label.text,
                                        fill: CorridorStyle.line(label.altIndex),
                                        casing: CorridorStyle.casing(dark: dark))
                v.displayPriority = label.altIndex == 0 ? .required
                                  : label.altIndex == 1 ? .defaultHigh : .defaultLow
                v.collisionMode = .rectangle
                v.canShowCallout = false
                // A number claiming "now" that is older than the poll cycle
                // gets the same treatment as its row in the NOW card: dimmed,
                // not hidden — it's the only reading that corridor has.
                v.alpha = label.stale ? 0.7 : 1
                v.accessibilityLabel = label.spoken
                return v
            case let pin as EndpointPin:
                let v = mapView.dequeueReusableAnnotationView(withIdentifier: "endpoint")
                    ?? MKAnnotationView(annotation: pin, reuseIdentifier: "endpoint")
                v.annotation = pin
                let t = Theme.current(dark ? .dark : .light)
                v.image = MapGlyph.endpoint(pin.kind == "home" ? "house.fill" : "building.2.fill",
                                            ink: UIColor(t.ink),
                                            ground: CorridorStyle.casing(dark: dark))
                v.displayPriority = .required
                v.collisionMode = .circle
                v.canShowCallout = true
                return v
            default:
                return nil
            }
        }
    }
}
