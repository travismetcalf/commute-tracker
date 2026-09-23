import Foundation
import OSLog
import Security
import WatchConnectivity
import WidgetKit

/// Receives the bearer token + server URL from the iPhone (WCSession
/// applicationContext survives the watch being offline at send time) and
/// persists them in the SHARED keychain group so the complication extension
/// can read them too. Also owns the summary fetch for the app UI.
final class WatchStore: NSObject, ObservableObject {
    static let shared = WatchStore()

    @Published var summary: WatchSummary?
    @Published var status = "—"

    /// The last ingest's write verdict, when it's something the wearer should
    /// see — nil after a clean shared-group write. See `ingest(_:)` for why
    /// this is a published banner rather than an alert.
    @Published var keychainNotice: WriteResult?

    static let accessGroup = "8F62W24942.com.travismetcalf.commute.shared"
    private static let service = "com.travismetcalf.commute.watch"
    private static let log = Logger(subsystem: "com.travismetcalf.commute.watchkitapp",
                                    category: "keychain")

    override init() {
        super.init()
        guard WCSession.isSupported() else { return }
        WCSession.default.delegate = self
        WCSession.default.activate()
    }

    // ── keychain ──────────────────────────────────────────────────────────
    //
    // Same shape as the phone's Core/Keychain.swift, and for the same reason:
    // the shared group is a device luxury, not a guarantee. A Simulator build
    // signed "to Run Locally" gets an EMPTY entitlements blob, so every
    // group-scoped SecItem call returns errSecMissingEntitlement no matter how
    // correct project.yml is. This used to discard both statuses, so `ingest`
    // believed the phone's token had landed while `configured` reported false
    // and the status line blamed the phone for a signing problem.
    //
    // A build that cannot SHARE the token can still STORE it, so writes fall
    // back to the app-private item: the watch app keeps working and only the
    // complication goes dark — which is the honest scope of such a build.

    /// What actually happened on the last write. Mirrors the phone's
    /// `Keychain.WriteResult`.
    enum WriteResult: Equatable {
        case stored              // in the shared group — the complication sees it
        case storedAppOnly       // this build has no group entitlement
        case failed(OSStatus)
    }

    private static func query(_ account: String, group: Bool) -> [String: Any] {
        var q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if group { q[kSecAttrAccessGroup as String] = accessGroup }
        return q
    }

    private static func value(_ account: String, group: Bool) -> String? {
        var q = query(account, group: group)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private static func add(_ account: String, _ value: String, group: Bool) -> OSStatus {
        var q = query(account, group: group)
        q[kSecValueData as String] = Data(value.utf8)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        return SecItemAdd(q as CFDictionary, nil)
    }

    /// Prefers the shared group, falling back to whatever a group-less build
    /// wrote app-privately.
    static func read(_ account: String) -> String? {
        if let shared = value(account, group: true) { return shared }
        guard let local = value(account, group: false) else { return nil }
        // Promote the fallback item once a build that HAS the entitlement runs
        // — but drop the private copy only after the group write actually
        // lands. Deleting first would cost the token outright on a build
        // without the entitlement: the write fails, the delete doesn't.
        if add(account, local, group: true) == errSecSuccess {
            SecItemDelete(query(account, group: false) as CFDictionary)
        }
        return local
    }

    /// Stores `value` under `account`, replacing any existing copy in either
    /// scope. Never silently discards a refusal — the caller gets the verdict.
    @discardableResult
    static func write(_ account: String, _ value: String) -> WriteResult {
        SecItemDelete(query(account, group: true) as CFDictionary)
        SecItemDelete(query(account, group: false) as CFDictionary)
        let shared = add(account, value, group: true)
        if shared == errSecSuccess { return .stored }
        guard shared == errSecMissingEntitlement else { return .failed(shared) }
        let local = add(account, value, group: false)
        return local == errSecSuccess ? .storedAppOnly : .failed(local)
    }

    /// An honest read-back — never a flag set from "we asked for a write".
    /// The assert buttons gate on this.
    var configured: Bool {
        guard let token = Self.read("token") else { return false }
        return !token.isEmpty
    }

    // ── data ──────────────────────────────────────────────────────────────
    @MainActor
    func refresh() async {
        guard let s = await WatchAPI.summary() else {
            // A refused write outranks the other two explanations: the phone
            // may well have synced, and "open iPhone app" would send the
            // wearer chasing a problem that isn't on the phone. An app-private
            // fallback needs no such override — the token IS readable, so the
            // usual diagnosis holds and the banner carries the caveat.
            status = keychainNotice?.failureNote
                ?? (configured ? "server unreachable" : "open iPhone app to sync")
            return
        }
        summary = s
        status = "updated \(Date().formatted(date: .omitted, time: .shortened))"
        // The complication shares the keychain group and its own fetch, but
        // its 20-min timeline budget means a fresh install shows "—" for up
        // to a cycle. A reload here fills it seconds after the app fetches.
        WidgetCenter.shared.reloadAllTimelines()
    }
}

extension WatchStore: WCSessionDelegate {
    func session(_ session: WCSession, activationDidCompleteWith state: WCSessionActivationState,
                 error: Error?) {
        // Context can already be waiting from a send that happened while the
        // watch was out of reach.
        ingest(session.receivedApplicationContext)
    }

    func session(_ session: WCSession, didReceiveApplicationContext ctx: [String: Any]) {
        ingest(ctx)
    }

    /// Persists whatever the phone sent. Unlike the phone's Settings screen
    /// there is no tap to attach an alert to — this runs off a WCSession
    /// delegate callback, with the wearer's wrist very likely down — so the
    /// honest ceiling here is a log line for the developer plus a sticky
    /// banner in the UI. Failing silently is not on the menu: without the
    /// token every button and every fetch below is dead.
    private func ingest(_ ctx: [String: Any]) {
        var results: [WriteResult] = []
        if let token = ctx["token"] as? String, !token.isEmpty {
            results.append(Self.write("token", token))
        }
        if let url = ctx["serverURL"] as? String, !url.isEmpty {
            results.append(Self.write("serverURL", url))
        }
        let verdict = Self.worst(of: results)
        if let line = verdict?.logLine { Self.log.error("\(line, privacy: .public)") }
        Task { @MainActor in
            self.keychainNotice = verdict == .stored ? nil : verdict
            await self.refresh()
        }
    }

    /// The most degraded outcome across a batch — the one worth reporting.
    /// Nil when the phone sent nothing worth writing.
    private static func worst(of results: [WriteResult]) -> WriteResult? {
        if let failure = results.first(where: {
            if case .failed = $0 { return true } else { return false }
        }) { return failure }
        return results.contains(.storedAppOnly) ? .storedAppOnly : results.first
    }
}

extension WatchStore.WriteResult {
    /// Diagnostic detail for the log — the full OSStatus story.
    var logLine: String? {
        switch self {
        case .stored:
            return nil
        case .storedAppOnly:
            return "keychain group \(WatchStore.accessGroup) unavailable "
                + "(errSecMissingEntitlement); stored app-private instead — "
                + "the complication cannot read it"
        case .failed(let status):
            return "keychain write failed: \(SecStatus.describe(status))"
        }
    }

    /// What a 40 mm screen can hold. Nil when nothing needs saying.
    var watchNote: String? {
        switch self {
        case .stored: nil
        case .storedAppOnly: "saved to this app only — complication won't update"
        case .failed(let status): "keychain error \(status) — sync can't be saved"
        }
    }

    /// Only set when the token genuinely did not land anywhere, so callers can
    /// tell "degraded but working" from "broken".
    var failureNote: String? {
        if case .failed = self { return watchNote }
        return nil
    }

    var isFailure: Bool {
        if case .failed = self { return true }
        return false
    }
}

// ── server access (shared shape with the iOS widget) ──────────────────────

struct WatchSummary: Codable {
    struct Bucket: Codable { let avg_sec: Int?; let n: Int }
    struct Now: Codable { let travel_sec: Int?; let historic_sec: Int? }
    let direction: String
    let now: Now?
    let am_out: Bucket?
    let pm_in: Bucket?
}

enum WatchAPI {
    static func base() -> URL? {
        URL(string: WatchStore.read("serverURL") ?? "https://traffic.travismetcalf.com")
    }

    static func summary() async -> WatchSummary? {
        guard let token = WatchStore.read("token"),
              let url = base()?.appendingPathComponent("api/v1/summary") else { return nil }
        var req = URLRequest(url: url, timeoutInterval: 15)
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return try? JSONDecoder().decode(WatchSummary.self, from: data)
    }

    /// Assert buttons post straight to the ingest surface — same append-only
    /// event contract as the phone; the server derives trips.
    static func assert(kind: String) async -> Bool {
        guard let token = WatchStore.read("token"),
              let url = base()?.appendingPathComponent("api/v1/ingest/events") else { return false }
        let ts = Int(Date().timeIntervalSince1970)
        let event: [String: Any] = [
            "event_key": "watch-\(kind)-\(ts / 10 * 10)",
            "kind": kind, "source": "intent",
            "device_ts": ts,
        ]
        var req = URLRequest(url: url, timeoutInterval: 15)
        req.httpMethod = "POST"
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["events": [event]])
        guard let (_, resp) = try? await URLSession.shared.data(for: req) else { return false }
        return (resp as? HTTPURLResponse)?.statusCode == 200
    }
}
