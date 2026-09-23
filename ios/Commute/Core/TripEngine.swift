import Foundation
import CoreLocation
import CoreMotion
import BackgroundTasks

/// Orchestrates the four layers:
///   1. WAKE    — CLMonitor geofences (home/office from /api/v1/config) +
///                significant-location-change backstop + visits (advisory)
///   2. CONFIRM — Core Motion reconciliation from the last checkpoint (the
///                coprocessor records continuously and keeps ~7 days, so a
///                missed wake becomes a recoverable gap, not lost data)
///   3. RECORD  — CLLocationManager polyline capture during a confirmed drive
///   4. OUTBOX  — durable queue, flushed inline on every wake (see Outbox)
///
/// Design rule carried over from the server: raw events are truth, pairing is
/// the SERVER's job (app/derive.py). The app never decides what was a commute.
final class TripEngine: NSObject {
    static let shared = TripEngine()

    private let slcManager = CLLocationManager()   // wake backstop + visits
    private var serviceSession: CLServiceSession?
    private var monitorTask: Task<Void, Never>?
    private var monitorRef: CLMonitor?
    private let recorder = RouteRecorder()
    private let motion = CMMotionActivityManager()

    private let defaults = UserDefaults.standard
    private var lastCheckpoint: Date {
        get { Date(timeIntervalSince1970: defaults.double(forKey: "motionCheckpoint")) }
        set { defaults.set(newValue.timeIntervalSince1970, forKey: "motionCheckpoint") }
    }
    /// The geofence_leave event key that opened the current candidate trip —
    /// the server pairs the eventual track upload to it.
    private var openLeaveKey: String? {
        get { defaults.string(forKey: "openLeaveKey") }
        set {
            defaults.set(newValue, forKey: "openLeaveKey")
            // Companion timestamp: lets a later launch tell a live session from
            // one orphaned by a jetsam days ago.
            defaults.set(newValue == nil ? 0 : Date().timeIntervalSince1970,
                         forKey: "openLeaveAt")
        }
    }
    private var openLeaveAt: Date? {
        let t = defaults.double(forKey: "openLeaveAt")
        return t > 0 ? Date(timeIntervalSince1970: t) : nil
    }
    /// Mirrors the recorder's hard cap — a session older than this is dead.
    private let maxTripSeconds: TimeInterval = 2 * 3600

    /// Claim the trip identity for `key` unless a live session already owns it.
    /// FIRST writer wins: when a Shortcuts automation opened the drive, that
    /// event is the trip's identity server-side (derive.py keeps the opening
    /// event and treats a following crossing as refinement), so overwriting it
    /// here keys the uploaded polyline to an event that is never a trip start —
    /// and the upload 409s forever. A session older than the hard cap belongs
    /// to a drive that never closed (jetsam, force-quit) and is never inherited.
    private func claimLeaveKey(_ key: String) {
        if let opened = openLeaveAt, Date().timeIntervalSince(opened) > maxTripSeconds {
            openLeaveKey = nil
        }
        if openLeaveKey == nil { openLeaveKey = key }
    }

    /// Last state seen per region, so a re-arm asserts what we already know.
    /// With `assuming: .unknown`, iOS reports EVERY region's current state as a
    /// fresh transition on each arm — and `attachMonitor` runs on every launch,
    /// so each cold start emitted a burst of physically impossible events
    /// (leaving three places two seconds apart) that then paired into bogus
    /// trips. Only a region genuinely never observed should start unknown.
    private var lastStates: [String: String] {
        get { defaults.dictionary(forKey: "monitorStates") as? [String: String] ?? [:] }
        set { defaults.set(newValue, forKey: "monitorStates") }
    }

    private func rememberState(_ state: CLMonitor.Event.State, for id: String) {
        var s = lastStates
        switch state {
        case .satisfied:   s[id] = "satisfied"
        case .unsatisfied: s[id] = "unsatisfied"
        default:           return   // never write .unknown over real knowledge
        }
        lastStates = s
    }

    private func assumedState(for id: String) -> CLMonitor.Event.State {
        switch lastStates[id] {
        case "satisfied":   return .satisfied
        case "unsatisfied": return .unsatisfied
        default:            return .unknown
        }
    }

    // ── launch-critical bootstrap (called synchronously from AppDelegate) ──
    // @MainActor is free here: UIApplicationDelegate is main-actor isolated, so
    // this stays a SYNCHRONOUS call at launch — the invariant that matters.
    @MainActor
    func bootstrapAtLaunch(launchedForLocation: Bool) {
        // 1. Re-take the service session IMMEDIATELY. iOS 18 forgets implicit
        //    sessions a few seconds after launch; without this, Always auth
        //    stops yielding events and tracking dies silently.
        serviceSession = CLServiceSession(authorization: .always)

        // 2. Re-attach the monitor and resubscribe .events (also perishable).
        attachMonitor()

        // 3. SLC + visits must be re-asserted on every launch to keep the
        //    relaunch guarantee alive.
        slcManager.delegate = self
        slcManager.startMonitoringSignificantLocationChanges()
        slcManager.startMonitoringVisits()

        recorder.onFinish = { [weak self] points in self?.finishRecording(points: points) }

        if launchedForLocation {
            status("woken by location event")
        }
        // Every wake runs reconciliation — missed events become gaps to
        // repair, not losses.
        Task { await self.reconcileAndFlush() }
    }

    // ── layer 1: geofences ────────────────────────────────────────────────
    private func attachMonitor() {
        monitorTask?.cancel()
        monitorTask = Task {
            let monitor = await CLMonitor("commute")
            self.monitorRef = monitor
            // Sync conditions with the server's places (radii and monitoring
            // are tunable server-side). Identifier = place key.
            if let config = try? await ServerClient.fetchConfig() {
                await self.armGeofences(from: config, on: monitor)
            } else {
                let armed = await monitor.identifiers.count
                await MainActor.run { AppState.shared.monitorsArmed = armed }
            }

            // One subscription per instance, forever — cancelling and
            // resubscribing fails silently (documented CLMonitor gotcha).
            do {
                for try await event in await monitor.events {
                    await handleMonitorEvent(event)
                }
            } catch {
                await MainActor.run { AppState.shared.lastError = "monitor: \(error.localizedDescription)" }
            }
        }
    }

    private func handleMonitorEvent(_ event: CLMonitor.Event) async {
        rememberState(event.state, for: event.identifier)
        switch event.state {
        case .satisfied:      // entered the region
            let key = await Outbox.shared.record(kind: "geofence_enter",
                                                 place: event.identifier, source: "ios")
            _ = key
            status("entered \(event.identifier)")
            // If we are NOT recording, the app was jetsammed mid-drive: the
            // polyline is gone and finishRecording will never fire, so the
            // open leave-key would linger in UserDefaults and get attached to
            // some future trip's track. Close the session here instead.
            let wasRecording = await MainActor.run { recorder.isRecording }
            if !wasRecording { openLeaveKey = nil }
            await MainActor.run { recorder.stop(reason: "arrived \(event.identifier)") }
        case .unsatisfied:    // left the region
            let key = await Outbox.shared.record(kind: "geofence_leave",
                                                 place: event.identifier, source: "ios")
            claimLeaveKey(key)
            status("left \(event.identifier) — recording")
            await MainActor.run { recorder.start(accuracy: AppState.shared.gpsAccuracy) }
        default:
            break
        }
        await reconcileAndFlush()
    }

    // ── layer 2: motion reconciliation ────────────────────────────────────
    /// Query the coprocessor's history since the last checkpoint and emit
    /// drive segments as corroborating events. Runs on EVERY wake.
    private func reconcileMotion() async {
        guard CMMotionActivityManager.isActivityAvailable() else { return }
        // Pre-consent queries return nothing but WOULD advance the checkpoint,
        // silently burning the 7-day replay buffer. Never touch it until
        // authorized (.notDetermined prompting happens via requestMotionPermission).
        guard CMMotionActivityManager.authorizationStatus() == .authorized else { return }
        let from = max(lastCheckpoint, Date().addingTimeInterval(-6 * 86400))
        let to = Date()
        guard to.timeIntervalSince(from) > 120 else { return }

        // Capture the ERROR too: advancing the checkpoint after a failed query
        // silently discards that window forever, defeating the whole point of
        // layer 2 ("a missed wake is a recoverable gap, not a lost trip").
        let (activities, queryError): ([CMMotionActivity], Error?) =
            await withCheckedContinuation { cont in
                motion.queryActivityStarting(from: from, to: to, to: .main) { acts, err in
                    cont.resume(returning: (acts ?? [], err))
                }
            }
        if queryError != nil {
            await MainActor.run {
                AppState.shared.lastError = "motion query failed — will retry"
            }
            return   // checkpoint NOT advanced; this window is re-queried later
        }
        // Contiguous automotive runs at medium/high confidence. A car at a
        // light reports automotive AND stationary — that's still driving.
        var segStart: Date?
        var lastAuto: Date?
        func closeSegment(_ end: Date) async {
            guard let s = segStart, end.timeIntervalSince(s) >= 300 else { segStart = nil; return }
            await Outbox.shared.record(kind: "motion_segment", place: nil,
                                       source: "motion_backfill", confidence: "high",
                                       deviceTS: s)
            segStart = nil
        }
        for a in activities where a.confidence != .low {
            if a.automotive {
                if segStart == nil { segStart = a.startDate }
                lastAuto = a.startDate
            } else if !a.stationary {           // walking/cycling ends a drive
                if let e = lastAuto { await closeSegment(e) }
            }
        }
        if let e = lastAuto { await closeSegment(e) }
        lastCheckpoint = to
    }

    func reconcileAndFlush() async {
        await reconcileMotion()
        await Outbox.shared.flush()
    }

    // ── layer 3: recording lifecycle ──────────────────────────────────────
    private func finishRecording(points: [[Double]]) {
        guard points.count >= 2, let key = openLeaveKey else { return }
        Task {
            await Outbox.shared.recordTrack(startEventKey: key, points: points)
            openLeaveKey = nil
            await Outbox.shared.flush()
        }
    }

    /// Explicitly trigger the Motion & Fitness permission prompt (first query
    /// from .notDetermined raises it), then reconcile if granted.
    func requestMotionPermission() async {
        guard CMMotionActivityManager.isActivityAvailable() else { return }
        _ = await withCheckedContinuation { (cont: CheckedContinuation<[CMMotionActivity], Never>) in
            motion.queryActivityStarting(from: Date().addingTimeInterval(-3600),
                                         to: Date(), to: .main) { acts, _ in
                cont.resume(returning: acts ?? [])
            }
        }
        await MainActor.run { AppState.shared.refreshPermissions() }
        await reconcileAndFlush()
    }

    /// Re-fetch server config and re-arm geofences. Callable any time —
    /// after token save, after a successful connection test, on foreground
    /// when nothing is armed. attachMonitor's one launch-time fetch is not
    /// enough: a missing token at launch left "Geofences armed: 0" forever.
    func resyncConfig() async {
        guard let monitor = monitorRef,
              let config = try? await ServerClient.fetchConfig() else { return }
        await armGeofences(from: config, on: monitor)
    }

    /// Arm one geofence per MONITORED place, and remove every condition that no
    /// longer belongs — a place the user retired at /settings, or one they
    /// deleted outright. Both callers share this so the "monitored" filter and
    /// the removal set can never disagree: arming from the full list while
    /// removing from the filtered one would drop every fence on the first sync.
    ///
    /// The count reported to the UI is the number ARMED, not the number of
    /// places, so "Geofences armed: 2" stays a fact about the phone.
    private func armGeofences(from config: ServerClient.Config,
                              on monitor: CLMonitor) async {
        let watched = config.places.filter(\.isMonitored)
        let wanted = Set(watched.map(\.key))
        var states = lastStates
        for id in await monitor.identifiers where !wanted.contains(id) {
            await monitor.remove(id)
            states[id] = nil    // re-arming it later must start from unknown
        }
        lastStates = states
        for p in watched {
            let cond = CLMonitor.CircularGeographicCondition(
                center: CLLocationCoordinate2D(latitude: p.lat, longitude: p.lon),
                radius: CLLocationDistance(p.geofence_radius_m))
            // Assert what we already know instead of .unknown — see lastStates.
            await monitor.add(cond, identifier: p.key, assuming: assumedState(for: p.key))
        }
        await MainActor.run { AppState.shared.monitorsArmed = watched.count }
    }

    // ── manual / intent entry points ──────────────────────────────────────
    func assertDriveStarted() async {
        let key = await Outbox.shared.record(kind: "intent_assert", place: nil, source: "intent")
        claimLeaveKey(key)
        await MainActor.run {
            recorder.start(accuracy: AppState.shared.gpsAccuracy)
            AppState.shared.engineStatus = "drive asserted — recording"
        }
        await Outbox.shared.flush()
    }

    func assertArrived() async {
        await Outbox.shared.record(kind: "drive_end", place: nil, source: "intent")
        await MainActor.run { recorder.stop(reason: "arrival asserted") }
        await Outbox.shared.flush()
    }

    // ── nightly backstop flush ────────────────────────────────────────────
    func scheduleNightlyFlush() {
        let request = BGProcessingTaskRequest(identifier: Outbox.flushTaskID)
        request.requiresNetworkConnectivity = true
        request.earliestBeginDate = Date(timeIntervalSinceNow: 6 * 3600)
        try? BGTaskScheduler.shared.submit(request)
    }

    func handleFlushTask(_ task: BGProcessingTask) {
        scheduleNightlyFlush()   // ALWAYS resubmit first — forgetting this
                                 // permanently opts the app out of future runs
        let work = Task {
            await reconcileAndFlush()
            task.setTaskCompleted(success: true)
        }
        task.expirationHandler = { work.cancel() }
    }

    private func status(_ s: String) {
        Task { await MainActor.run { AppState.shared.engineStatus = s } }
    }
}

// ── SLC + visits delegate (wake backstop; advisory only) ──────────────────
extension TripEngine: CLLocationManagerDelegate {
    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        // A significant-change fire is just a wake trigger: reconcile.
        Task { await reconcileAndFlush() }
    }

    func locationManager(_ manager: CLLocationManager, didVisit visit: CLVisit) {
        // Advisory timestamps only — never authoritative (the replay bug
        // FB12107066 can redeliver old visits; dedupe by content in the key).
        let arrived = visit.arrivalDate != .distantPast
        let departed = visit.departureDate != .distantFuture
        let ts = departed ? visit.departureDate : (arrived ? visit.arrivalDate : Date())
        guard arrived || departed else { return }
        Task {
            await Outbox.shared.record(kind: "motion_segment", place: nil,
                                       source: "motion_backfill", confidence: "low",
                                       lat: visit.coordinate.latitude,
                                       lon: visit.coordinate.longitude,
                                       acc: Int(visit.horizontalAccuracy),
                                       deviceTS: ts)
            await Outbox.shared.flush()
        }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        Task { await MainActor.run { AppState.shared.refreshPermissions() } }
    }
}
