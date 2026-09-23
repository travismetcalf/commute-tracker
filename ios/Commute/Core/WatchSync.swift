import Foundation
import WatchConnectivity

/// Pushes the bearer token + server URL to the paired watch. applicationContext
/// is the right channel: latest-value-wins, delivered whenever the watch is
/// reachable, survives both apps being closed. Called at launch and whenever
/// the token or URL changes.
final class WatchSync: NSObject, WCSessionDelegate {
    static let shared = WatchSync()

    func activate() {
        guard WCSession.isSupported() else { return }
        WCSession.default.delegate = self
        WCSession.default.activate()
    }

    @MainActor
    func push() {
        guard WCSession.isSupported(),
              WCSession.default.activationState == .activated,
              let token = Keychain.token, !token.isEmpty else { return }
        try? WCSession.default.updateApplicationContext([
            "token": token,
            "serverURL": AppState.shared.serverURL,
        ])
    }

    // Delegate minimum: push once the session comes up.
    func session(_ session: WCSession, activationDidCompleteWith state: WCSessionActivationState,
                 error: Error?) {
        Task { await self.push() }
    }
    func sessionDidBecomeInactive(_ session: WCSession) {}
    func sessionDidDeactivate(_ session: WCSession) { session.activate() }
}
