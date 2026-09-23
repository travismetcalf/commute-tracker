import UIKit

/// Handles Home Screen quick actions ("I'm driving now" / "I've arrived",
/// declared statically in Info.plist). Cold launches deliver the item in
/// connectionOptions; warm launches call performActionFor.
final class QuickActionSceneDelegate: NSObject, UIWindowSceneDelegate {
    func scene(_ scene: UIScene, willConnectTo session: UISceneSession,
               options connectionOptions: UIScene.ConnectionOptions) {
        if let item = connectionOptions.shortcutItem {
            handle(item)
        }
    }

    func windowScene(_ windowScene: UIWindowScene,
                     performActionFor shortcutItem: UIApplicationShortcutItem,
                     completionHandler: @escaping (Bool) -> Void) {
        handle(shortcutItem)
        completionHandler(true)
    }

    private func handle(_ item: UIApplicationShortcutItem) {
        switch item.type {
        case "com.travismetcalf.commute.drive-start":
            Task { await TripEngine.shared.assertDriveStarted() }
        case "com.travismetcalf.commute.arrive":
            Task { await TripEngine.shared.assertArrived() }
        default:
            break
        }
    }
}
