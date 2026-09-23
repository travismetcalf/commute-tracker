import Foundation
import UIKit

/// Bearer-token client for the server's /api/v1 surface. Mirrors the contract
/// verified by curl in Phase 1: batch ingest is idempotent on event_key and a
/// duplicate returns 200 {"status":"duplicate"} — never an error.
struct ServerClient {
    enum ClientError: Error, LocalizedError {
        case badURL, http(Int), noToken
        var errorDescription: String? {
            switch self {
            case .badURL: "Server URL is invalid"
            case .http(let code): code == 401 ? "Token rejected (401)" : "Server error \(code)"
            case .noToken: "No API token — paste it in Settings"
            }
        }
    }

    @MainActor static func baseURL() throws -> URL {
        guard let url = URL(string: AppState.shared.serverURL) else { throw ClientError.badURL }
        return url
    }

    private static func request(_ path: String, method: String = "GET",
                                body: Data? = nil) async throws -> (Data, Int) {
        guard let token = Keychain.token, !token.isEmpty else { throw ClientError.noToken }
        var req = URLRequest(url: try await url(for: path), timeoutInterval: 20)
        req.httpMethod = method
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = body
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        return (data, code)
    }

    /// `appending(path:)` percent-encodes its whole argument AS a path, so a
    /// caller's "?" becomes %3F and the query never separates: the map's
    /// /api/v1/route?route=…&alts=1 arrived as one long path and the route
    /// simply didn't match server-side. Split the query off and let
    /// `appending(queryItems:)` encode it as a query.
    private static func url(for path: String) async throws -> URL {
        let parts = path.split(separator: "?", maxSplits: 1)
        let url = try await baseURL().appending(path: String(parts[0]))
        guard parts.count == 2 else { return url }
        return url.appending(queryItems: parts[1].split(separator: "&").map { pair in
            let kv = pair.split(separator: "=", maxSplits: 1)
            return URLQueryItem(name: String(kv[0]),
                                value: kv.count == 2 ? String(kv[1]) : nil)
        })
    }

    // ── config: places + geofence radii, so nothing is hardcoded ──────────
    struct Place: Codable { let key: String; let label: String
        let lat: Double; let lon: Double; let geofence_radius_m: Int
        /// "home" | "office", decided server-side so the map pins on iOS and
        /// on the web can't disagree about which end is which. Optional:
        /// older servers omit it, and a pin is worth less than a geofence.
        let kind: String?
        /// Whether to arm a geofence here. A place you no longer drive to is
        /// still SENT — the baseline corridors it anchors need their map
        /// coordinates — so the phone must filter rather than the payload.
        /// nil means a server that predates the column: geofence it, which is
        /// what that server was already asking for.
        let monitored: Bool?
        var isMonitored: Bool { monitored ?? true } }
    /// Only what the map joins on. The trip engine works off `places`; this is
    /// the pre-resolved origin/dest pair so a pin never needs a second lookup.
    struct Route: Codable {
        let slug: String
        let label: String?          // optional: older servers omit it
        let origin: Place?
        let dest: Place?
    }
    /// One channel's switch on a notification type: `notify.setting_key()`'s
    /// output plus its current value and shipping default. `settingKey` is
    /// what every write uses — the client never rebuilds
    /// `notify_{type}_{channel}` itself, which is the duplication
    /// `notify.payload()` exists to remove (see notify.py's docstring).
    struct NotificationSwitch: Codable {
        let channel: String
        let settingKey: String
        let enabled: Bool
        let defaultValue: String

        enum CodingKeys: String, CodingKey {
            case channel, enabled
            case settingKey = "setting_key"
            case defaultValue = "default"
        }
    }
    /// One entry in the server's notification catalog (`notify.CATALOG`),
    /// as rendered by `notify.payload()`. Read-only from here: the catalog
    /// is server-owned, so nothing in this struct is client-authored.
    struct NotificationType: Codable {
        let key: String
        let label: String
        let help: String
        let channels: [String]
        let switches: [NotificationSwitch]
    }
    struct Config: Codable {
        let places: [Place]
        let routes: [Route]?                      // optional: older servers omit it
        let max_trip_hours: Int
        let noise_under_sec: Int
        let alerts: [String: String]?              // optional: older servers omit it
        let notifications: [NotificationType]?      // optional: older servers omit it

        private enum CodingKeys: String, CodingKey {
            case places, routes, max_trip_hours, noise_under_sec, alerts, notifications
        }

        /// `notifications` alone decodes element-wise (below) so one malformed
        /// or old-shaped entry can't take the rest of Config down with it.
        ///
        /// F1: `notifications` being OPTIONAL only covers the key being
        /// MISSING. It does not cover the key being present in the shape a
        /// server predating the `switches` field actually sends today
        /// (`main`'s `notify.payload()` still emits `"enabled": {ch: bool}`,
        /// not `"switches": [...]`) — decoding `NotificationType` there hits
        /// `DecodingError.keyNotFound("switches")`, and because the whole
        /// struct decoded via the synthesized member-wise initializer, that
        /// one array element failing threw the WHOLE `Config` decode,
        /// silently killing `places`/`routes` too — which is what arms
        /// geofences. A client built here (new `Config`, new `notifications`
        /// key) hitting the old (`main`) server shape must not lose
        /// geofencing over a notification catalog it can degrade gracefully
        /// without.
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            places = try c.decode([Place].self, forKey: .places)
            routes = try c.decodeIfPresent([Route].self, forKey: .routes)
            max_trip_hours = try c.decode(Int.self, forKey: .max_trip_hours)
            noise_under_sec = try c.decode(Int.self, forKey: .noise_under_sec)
            alerts = try c.decodeIfPresent([String: String].self, forKey: .alerts)
            let raw = try c.decodeIfPresent([FailableDecodable<NotificationType>].self,
                                            forKey: .notifications)
            notifications = raw?.compactMap(\.base)
        }
    }

    /// Decodes `Base`, swallowing per-element failure instead of propagating
    /// it to the array (and everything around the array) decoding it. Used
    /// only for `Config.notifications` (see F1) — a catalog entry the server
    /// sent in a shape this client doesn't understand is dropped, not fatal.
    private struct FailableDecodable<Base: Decodable>: Decodable {
        let base: Base?
        init(from decoder: Decoder) throws {
            let container = try decoder.singleValueContainer()
            base = try? container.decode(Base.self)
        }
    }

    static func fetchConfig() async throws -> Config {
        let (data, code) = try await request("/api/v1/config")
        guard code == 200 else { throw ClientError.http(code) }
        return try JSONDecoder().decode(Config.self, from: data)
    }

    // ── batch event ingest ────────────────────────────────────────────────
    struct IngestResult: Codable { let event_key: String?; let status: String; let reason: String? }
    struct IngestResponse: Codable { let results: [IngestResult] }

    static func ingest(events: [[String: Any]]) async throws -> IngestResponse {
        let body = try JSONSerialization.data(withJSONObject: ["events": events])
        let (data, code) = try await request("/api/v1/ingest/events", method: "POST", body: body)
        guard code == 200 else { throw ClientError.http(code) }
        return try JSONDecoder().decode(IngestResponse.self, from: data)
    }

    // ── route polyline for a completed trip ───────────────────────────────
    /// Returns true when accepted; false when the server hasn't derived the
    /// trip yet (409 trip_not_found) — keep it queued and retry after the
    /// server's next derive cycle.
    static func uploadTrack(startEventKey: String, points: [[Double]]) async throws -> Bool {
        let body = try JSONSerialization.data(withJSONObject:
            ["start_event_key": startEventKey, "points": points])
        let (_, code) = try await request("/api/v1/ingest/track", method: "POST", body: body)
        if code == 200 { return true }
        if code == 409 { return false }
        throw ClientError.http(code)
    }

    static func registerDevice(token: String, env: String) async throws {
        let body = try JSONSerialization.data(withJSONObject: [
            "device_token": token, "apns_env": env,
            "bundle_id": "com.travismetcalf.commute",
            "os_version": await UIDevice.current.systemVersion,
        ])
        _ = try await request("/api/v1/devices", method: "POST", body: body)
    }

    /// POST to `/api/v1/settings` — alert windows (String values) or
    /// notification switches (Bool values); the server normalises either
    /// (`main._norm_setting_value`) and validates by key. Generic on purpose:
    /// one settings-write path for every native view that edits server
    /// settings, so the whitelist-and-validate discipline lives in one place
    /// (the server) rather than being re-decided per caller.
    static func saveSettings(_ values: [String: Any]) async -> Bool {
        guard let body = try? JSONSerialization.data(withJSONObject: values),
              let (_, code) = try? await request("/api/v1/settings",
                                                 method: "POST", body: body) else { return false }
        return code == 200
    }

    static func postVerdict(tripId: Int, verdict: String) async -> Bool {
        guard let body = try? JSONSerialization.data(withJSONObject: ["verdict": verdict]),
              let (_, code) = try? await request("/api/v1/trips/\(tripId)/verdict",
                                                 method: "POST", body: body) else { return false }
        return code == 200
    }

    static func raw(_ path: String) async throws -> (Data, Int) {
        try await request(path)
    }

    static func checkConnection() async -> String {
        do {
            _ = try await fetchConfig()
            return "connected"
        } catch ClientError.noToken {
            return "no token — paste the bearer token from your password manager"
        } catch ClientError.http(let code) {
            return code == 401 ? "token rejected (401)" : "server error \(code)"
        } catch {
            return "unreachable: \(error.localizedDescription)"
        }
    }
}
