import Foundation
import CoreLocation

/// Layer 3: polyline capture during a confirmed drive. Uses the legacy
/// CLLocationManager API deliberately — CLLocationUpdate.liveUpdates has no
/// distanceFilter/desiredAccuracy, and power tuning matters here.
/// MainActor-confined: `points` is appended from the CLLocationManager
/// delegate (main) while stop() reads and clears it — off-main stops raced
/// that array and invalidated a main-runloop Timer from a background thread.
@MainActor
final class RouteRecorder: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private var points: [[Double]] = []
    private var lastMovement = Date()
    private var startedAt: Date?
    private var stopTimer: Timer?

    var onFinish: (([[Double]]) -> Void)?

    /// Safety valve: a stuck session must never run all day.
    private let hardCapSeconds: TimeInterval = 2 * 3600
    /// Stationary this long = the drive is over (parking, garage).
    private let stationaryCutoff: TimeInterval = 3 * 60

    override nonisolated init() {
        super.init()
        manager.delegate = self
        manager.activityType = .automotiveNavigation
        // Auto-pause ENDS background delivery until the app is relaunched —
        // Apple's documented recovery is prompting the user to reopen the
        // app, which is exactly the per-trip interaction we refuse to have.
        manager.pausesLocationUpdatesAutomatically = false
        manager.distanceFilter = 40
    }

    var isRecording: Bool { startedAt != nil }

    func start(accuracy: GPSAccuracy) {
        guard startedAt == nil else { return }
        guard manager.authorizationStatus == .authorizedAlways else { return }
        points = []
        startedAt = Date()
        lastMovement = Date()
        manager.desiredAccuracy = accuracy.clAccuracy
        manager.allowsBackgroundLocationUpdates = true
        manager.startUpdatingLocation()
        armStopTimer()
        Task { await MainActor.run { AppState.shared.isRecording = true } }
    }

    func stop(reason: String) {
        guard startedAt != nil else { return }
        manager.stopUpdatingLocation()
        manager.allowsBackgroundLocationUpdates = false
        stopTimer?.invalidate()
        let captured = points
        points = []
        startedAt = nil
        Task { await MainActor.run { AppState.shared.isRecording = false } }
        onFinish?(simplify(captured))
    }

    private func armStopTimer() {
        stopTimer?.invalidate()
        stopTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            guard let self, let started = self.startedAt else { return }
            if Date().timeIntervalSince(started) > self.hardCapSeconds {
                self.stop(reason: "hard cap")
            } else if Date().timeIntervalSince(self.lastMovement) > self.stationaryCutoff {
                self.stop(reason: "stationary")
            }
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard startedAt != nil else { return }
        for loc in locations where loc.horizontalAccuracy > 0 && loc.horizontalAccuracy < 200 {
            points.append([
                (loc.coordinate.longitude * 1e5).rounded() / 1e5,
                (loc.coordinate.latitude * 1e5).rounded() / 1e5,
                loc.timestamp.timeIntervalSince1970.rounded(),
            ])
            if loc.speed > 1 { lastMovement = Date() }
        }
    }

    /// Cheap thinning so a 45-min drive is a few hundred points, not thousands
    /// (the server caps track uploads at 20k points).
    private func simplify(_ pts: [[Double]]) -> [[Double]] {
        guard pts.count > 2000 else { return pts }
        let stride = pts.count / 1000
        return pts.enumerated().compactMap { $0.offset % stride == 0 ? $0.element : nil }
    }
}
