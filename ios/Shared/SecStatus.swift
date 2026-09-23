import Foundation
import Security

/// Renders an OSStatus as something a reader can act on, so a message says
/// what the number MEANS instead of leaving whoever hit it to go look up
/// -34018 in the middle of debugging.
///
/// This lives outside `Commute/` because the phone and the watch are separate
/// targets on separate platforms and share no other code: both store the
/// bearer token in the same access group, both hit the same
/// errSecMissingEntitlement wall on a build whose entitlements blob was
/// stripped, and both were formatting the status their own way. Anything added
/// to `Shared/` compiles into BOTH — keep it free of UIKit/WatchKit and of
/// anything platform-specific.
enum SecStatus {
    static func describe(_ status: OSStatus) -> String {
        let name = status == errSecMissingEntitlement ? "errSecMissingEntitlement " : ""
        let detail = (SecCopyErrorMessageString(status, nil) as String?)?
            .trimmingCharacters(in: CharacterSet(charactersIn: " ."))
        return detail.map { "\(name)\(status): \($0)" } ?? "\(name)\(status)"
    }
}
