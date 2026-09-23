import SwiftUI
import BackgroundTasks
import UserNotifications
import WidgetKit

@main
struct CommuteApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var appState = AppState.shared
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(appState)
                .preferredColorScheme(appState.appearance.colorScheme)
        }
        .onChange(of: scenePhase) { _, phase in
            // Foregrounding is a free, reliable flush trigger — the outbox
            // drains every time the app is opened, not just on location wakes.
            if phase == .active {
                Task {
                    if await AppState.shared.monitorsArmed == 0 {
                        await TripEngine.shared.resyncConfig()
                    }
                    await TripEngine.shared.reconcileAndFlush()
                }
            }
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        // ── LAUNCH-CRITICAL, SYNCHRONOUS ────────────────────────────────────
        // Core Location remembers our sessions for only a few seconds after a
        // background relaunch. Re-take the service session, re-attach the
        // monitor, and restart SLC BEFORE any async work — deferring any of
        // this is the #1 cause of "worked for a week then quietly stopped".
        TripEngine.shared.bootstrapAtLaunch(
            launchedForLocation: launchOptions?[.location] != nil
        )

        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: Outbox.flushTaskID, using: nil
        ) { task in
            TripEngine.shared.handleFlushTask(task as! BGProcessingTask)
        }
        TripEngine.shared.scheduleNightlyFlush()
        WatchSync.shared.activate()   // token rides to the watch
        // Badge was promoted from alternate to primary; clear a stale
        // selection of the now-nonexistent alternate name.
        if UIApplication.shared.alternateIconName == "AppIconBadge" {
            UIApplication.shared.setAlternateIconName(nil)
            AppState.shared.appIcon = nil
        }

        // Push: local notifications need permission; remote registration is
        // harmless before the server has an APNs key (tokens just accumulate).
        // Without this assignment neither willPresent nor didReceive is ever
        // called, however correctly they are implemented — the delegate was
        // never set before this change.
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            if granted {
                DispatchQueue.main.async { application.registerForRemoteNotifications() }
            }
        }
        return true
    }

    // Home Screen quick actions arrive on the SCENE delegate, which SwiftUI
    // doesn't expose — so hand the system a tiny custom one.
    func application(_ application: UIApplication,
                     configurationForConnecting session: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let config = UISceneConfiguration(name: nil, sessionRole: session.role)
        config.delegateClass = QuickActionSceneDelegate.self
        return config
    }

    func application(_ application: UIApplication,
                     didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        #if DEBUG
        let env = "sandbox"      // Xcode-installed builds use the APNs sandbox
        #else
        let env = "production"   // TestFlight/App Store builds use production
        #endif
        Task { try? await ServerClient.registerDevice(token: token, env: env) }
    }

    func application(_ application: UIApplication,
                     didReceiveRemoteNotification userInfo: [AnyHashable: Any]) async -> UIBackgroundFetchResult {
        // Silent pushes refresh the widget at exactly the moments that matter.
        WidgetCenter.shared.reloadAllTimelines()
        await TripEngine.shared.reconcileAndFlush()
        return .newData
    }

    /// Without this, iOS silently swallows a push that arrives while the app is
    /// in the foreground — you would be looking at the app and see nothing.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification)
        async -> UNNotificationPresentationOptions {
        [.banner, .sound, .list]
    }

    /// Tapping the notification opens the trip it is about.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse) async {
        let info = response.notification.request.content.userInfo
        guard let key = info["trip_key"] as? String else { return }
        await MainActor.run { AppState.shared.pendingTripKey = key }
    }
}
