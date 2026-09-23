import Foundation
import Security

/// Keychain wrapper for the API bearer token. It prefers the SHARED access
/// group, so the widget and watch extensions read the same item the app wrote.
///
/// The group is a device luxury, not a guarantee. Xcode signs Simulator builds
/// "to Run Locally", and with no provisioning profile to resolve
/// `$(AppIdentifierPrefix)` against it hands codesign an EMPTY entitlements
/// blob — so every group-scoped SecItem call comes back errSecMissingEntitlement
/// (-34018) no matter how correct project.yml is. A build that cannot SHARE the
/// token must still be able to STORE it, so writes fall back to the app-private
/// item. Extensions can't see it there, which is the honest scope of such a
/// build anyway — and `lastWrite` says so out loud rather than pretending.
enum Keychain {
    private static let service = "com.travismetcalf.commute"
    private static let account = "api-bearer-token"
    static let accessGroup = "8F62W24942.com.travismetcalf.commute.shared"

    /// What actually happened on the last write. This used to be discarded, so a
    /// refused save was indistinguishable from a successful one and Settings
    /// reported "stored in Keychain" over an empty keychain.
    enum WriteResult: Equatable {
        case stored              // in the shared group — extensions can read it
        case storedAppOnly       // this build has no group entitlement
        case failed(OSStatus)
        case cleared
    }

    private(set) static var lastWrite: WriteResult = .cleared

    /// Whether a token can actually be READ BACK, from either location.
    /// `lastWrite` reports what the last attempt claimed; this reports what is
    /// really there. UI that tells the user where their token stands must ask
    /// this one — an optimistic flag is how "stored in Keychain" ended up
    /// sitting above an empty keychain.
    static var hasToken: Bool {
        guard let t = token else { return false }
        return !t.isEmpty
    }

    private static func query(group: Bool) -> [String: Any] {
        var q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if group { q[kSecAttrAccessGroup as String] = accessGroup }
        return q
    }

    private static func read(group: Bool) -> String? {
        var q = query(group: group)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private static func write(_ value: String, group: Bool) -> OSStatus {
        var add = query(group: group)
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        return SecItemAdd(add as CFDictionary, nil)
    }

    static var token: String? {
        get {
            if let shared = read(group: true) { return shared }
            guard let local = read(group: false) else { return nil }
            // Promote a pre-sharing (or fallback-written) item into the group —
            // but drop the private copy ONLY once the group write has actually
            // landed. Deleting first cost the token outright on any build
            // without the entitlement: the write failed, the delete didn't.
            if write(local, group: true) == errSecSuccess {
                SecItemDelete(query(group: false) as CFDictionary)
            }
            return local
        }
        set {
            let sharedDelete = SecItemDelete(query(group: true) as CFDictionary)
            let localDelete = SecItemDelete(query(group: false) as CFDictionary)
            guard let value = newValue, !value.isEmpty else {
                // Neither delete's status means much on its own: a build with no
                // group entitlement always refuses the first, and a missing item
                // is not an error in either. Only a token that is STILL readable
                // means the clear failed — so check instead of assuming.
                if read(group: true) == nil && read(group: false) == nil {
                    lastWrite = .cleared
                } else {
                    lastWrite = .failed(localDelete == errSecSuccess ? sharedDelete : localDelete)
                }
                return
            }
            let shared = write(value, group: true)
            if shared == errSecSuccess {
                lastWrite = .stored
                return
            }
            guard shared == errSecMissingEntitlement else {
                lastWrite = .failed(shared)
                return
            }
            let local = write(value, group: false)
            lastWrite = local == errSecSuccess ? .storedAppOnly : .failed(local)
        }
    }
}
