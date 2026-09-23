import SwiftUI
import CoreLocation
import CoreMotion
import UserNotifications

enum Appearance: String, CaseIterable, Identifiable {
    case system, light, dark
    var id: String { rawValue }
    var colorScheme: ColorScheme? {
        switch self {
        case .system: nil
        case .light: .light
        case .dark: .dark
        }
    }
}

enum GPSAccuracy: String, CaseIterable, Identifiable {
    case coarse, precise
    var id: String { rawValue }
    /// Coarse (hundredMeters) can be served from cell/Wi-Fi — dramatically
    /// cheaper than continuous GPS (~8–15%/hr at Best). Precise is opt-in for
    /// when the polyline needs to distinguish parallel corridors.
    var clAccuracy: CLLocationAccuracy {
        switch self {
        case .coarse: kCLLocationAccuracyHundredMeters
        case .precise: kCLLocationAccuracyBest
        }
    }
}

/// App-wide observable state. Single source of truth for settings and the
/// engine/permission status the UI renders.
@MainActor
final class AppState: ObservableObject {
    static let shared = AppState()

    @AppStorage("serverURL") var serverURL: String = "https://traffic.travismetcalf.com"
    @AppStorage("appearanceRaw") private var appearanceRaw: String = Appearance.system.rawValue
    @AppStorage("gpsAccuracyRaw") private var gpsAccuracyRaw: String = GPSAccuracy.coarse.rawValue

    var appearance: Appearance {
        get { Appearance(rawValue: appearanceRaw) ?? .system }
        set { appearanceRaw = newValue.rawValue; objectWillChange.send() }
    }
    var gpsAccuracy: GPSAccuracy {
        get { GPSAccuracy(rawValue: gpsAccuracyRaw) ?? .coarse }
        set { gpsAccuracyRaw = newValue.rawValue; objectWillChange.send() }
    }

    // Live status the views render; TripEngine pushes updates.
    /// nil = primary icon; otherwise an alternate icon name. Seeded from iOS
    /// at launch so the picker highlights whatever is actually active.
    @Published var appIcon: String? = UIApplication.shared.alternateIconName

    @Published var engineStatus: String = "idle"
    @Published var isRecording: Bool = false
    @Published var monitorsArmed: Int = 0
    @Published var pendingEvents: Int = 0
    @Published var lastSync: Date?
    @Published var lastError: String?
    @Published var recentEvents: [OutboxEvent] = []
    @Published var kindSummaries: [Outbox.KindSummary] = []

    @Published var locationAuth: CLAuthorizationStatus = .notDetermined
    @Published var motionAuth: CMAuthorizationStatus = .notDetermined
    /// Real push-permission status from `UNUserNotificationCenter`, not
    /// inferred from whether the server-side switches are on. A switch can
    /// read "on" while iOS itself has denied delivery — this is the only
    /// source of truth for that, which is why Settings reads it rather than
    /// guessing from `notify_push_*` values. `.notDetermined` is the honest
    /// starting value: `refreshNotificationAuth()` has not resolved yet.
    @Published var notificationAuth: UNAuthorizationStatus = .notDetermined

    /// Set when a trip notification is tapped; consumed by ContentView, which
    /// switches to the Track tab. Optional because most launches have no deep
    /// link, and nil is the honest representation of that.
    @Published var pendingTripKey: String?

    func refreshPermissions() {
        locationAuth = CLLocationManager().authorizationStatus
        motionAuth = CMMotionActivityManager.authorizationStatus()
    }

    /// Separate from `refreshPermissions()` because `UNUserNotificationCenter`
    /// only exposes this asynchronously (no synchronous read exists) — callers
    /// await it explicitly rather than have a nominally-synchronous refresh
    /// silently kick off background work.
    func refreshNotificationAuth() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        notificationAuth = settings.authorizationStatus
    }

    func refreshOutboxStats() {
        Task {
            let stats = await Outbox.shared.stats()
            let summaries = await Outbox.shared.countsByKind()
            await MainActor.run {
                self.pendingEvents = stats.pending
                self.kindSummaries = summaries
            }
        }
    }
}
