import Foundation
import SQLite3

struct OutboxEvent: Identifiable {
    let id: Int64
    let eventKey: String
    let kind: String
    let placeKey: String?
    let deviceTS: Int64
    let sent: Bool
}

/// Durable outbox on raw SQLite (no dependencies). Layer 4 of the trip engine:
/// events are recorded locally FIRST, then flushed — on every location wake,
/// on foregrounding, and by the nightly BGProcessingTask. Rows are deleted
/// only after the server answers ok/duplicate, and every row carries a
/// client-generated idempotency key so replays are harmless.
actor Outbox {
    static let shared = Outbox()
    static let flushTaskID = "com.travismetcalf.commute.flush"

    private var db: OpaquePointer?

    private func open() {
        guard db == nil else { return }
        let dir = FileManager.default.urls(for: .applicationSupportDirectory,
                                           in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let path = dir.appending(path: "outbox.db").path
        guard sqlite3_open(path, &db) == SQLITE_OK else { db = nil; return }
        exec("PRAGMA journal_mode=WAL")
        exec("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                place_key TEXT,
                source TEXT NOT NULL,
                confidence TEXT,
                device_ts INTEGER NOT NULL,
                lat REAL, lon REAL, acc INTEGER,
                sent INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tracks (
                start_event_key TEXT PRIMARY KEY,
                points_json TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            """)
        // A background relaunch (geofence event after reboot) must be able to
        // write BEFORE first unlock would otherwise allow — without this, the
        // first post-reboot events throw. Commonly missed.
        try? FileManager.default.setAttributes(
            [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication],
            ofItemAtPath: path)
    }

    private func exec(_ sql: String) {
        sqlite3_exec(db, sql, nil, nil, nil)
    }

    // ── record ────────────────────────────────────────────────────────────
    /// Insert an event; returns its idempotency key. Duplicate keys are
    /// silently ignored (INSERT OR IGNORE) — recording must never fail loudly.
    @discardableResult
    func record(kind: String, place: String?, source: String,
                confidence: String? = nil,
                lat: Double? = nil, lon: Double? = nil, acc: Int? = nil,
                deviceTS: Date = Date()) -> String {
        open()
        let ts = Int64(deviceTS.timeIntervalSince1970)
        // Same synthesis rule the server documents: replays collide, distinct
        // events don't. The bucket absorbs double-fires — at 10s it missed the
        // ones that straddled a boundary (…118 and …123 landed in 110 and 120
        // and both shipped), so it is 60s: CLMonitor redelivers a crossing
        // within seconds, and two genuinely distinct crossings of the same
        // place inside a minute are dithering we want collapsed anyway.
        let key = "ios-\(kind)-\(place ?? "x")-\(ts / 60 * 60)"
        let sql = """
            INSERT OR IGNORE INTO events
              (event_key, kind, place_key, source, confidence, device_ts,
               lat, lon, acc, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return key }
        defer { sqlite3_finalize(stmt) }
        let T = SQLITE_TRANSIENT
        sqlite3_bind_text(stmt, 1, key, -1, T)
        sqlite3_bind_text(stmt, 2, kind, -1, T)
        place.map { sqlite3_bind_text(stmt, 3, $0, -1, T) } ?? sqlite3_bind_null(stmt, 3)
        sqlite3_bind_text(stmt, 4, source, -1, T)
        confidence.map { sqlite3_bind_text(stmt, 5, $0, -1, T) } ?? sqlite3_bind_null(stmt, 5)
        sqlite3_bind_int64(stmt, 6, ts)
        lat.map { sqlite3_bind_double(stmt, 7, $0) } ?? sqlite3_bind_null(stmt, 7)
        lon.map { sqlite3_bind_double(stmt, 8, $0) } ?? sqlite3_bind_null(stmt, 8)
        acc.map { sqlite3_bind_int(stmt, 9, Int32($0)) } ?? sqlite3_bind_null(stmt, 9)
        sqlite3_bind_int64(stmt, 10, Int64(Date().timeIntervalSince1970))
        sqlite3_step(stmt)
        return key
    }

    func recordTrack(startEventKey: String, points: [[Double]]) {
        open()
        guard let json = try? JSONSerialization.data(withJSONObject: points),
              let jsonStr = String(data: json, encoding: .utf8) else { return }
        var stmt: OpaquePointer?
        let sql = "INSERT OR REPLACE INTO tracks (start_event_key, points_json, created_at) VALUES (?,?,?)"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_text(stmt, 1, startEventKey, -1, SQLITE_TRANSIENT)
        sqlite3_bind_text(stmt, 2, jsonStr, -1, SQLITE_TRANSIENT)
        sqlite3_bind_int64(stmt, 3, Int64(Date().timeIntervalSince1970))
        sqlite3_step(stmt)
    }

    // ── flush ─────────────────────────────────────────────────────────────
    /// Push everything unsent. Called inline from location wakes (a few-KB
    /// POST finishes well inside the ~10s budget), on foreground, and from
    /// the nightly BGProcessingTask. Safe to call concurrently (actor).
    func flush() async {
        open()
        let pending = unsentEvents(limit: 100)
        if !pending.isEmpty {
            let payload = pending.map { e -> [String: Any] in
                var d: [String: Any] = ["event_key": e.key, "kind": e.kind,
                                        "source": e.source, "device_ts": e.ts]
                e.place.map { d["place_key"] = $0 }
                e.confidence.map { d["confidence"] = $0 }
                e.lat.map { d["lat"] = $0 }
                e.lon.map { d["lon"] = $0 }
                e.acc.map { d["acc"] = $0 }
                return d
            }
            do {
                let resp = try await ServerClient.ingest(events: payload)
                for r in resp.results where r.status == "ok" || r.status == "duplicate" || r.status == "rejected" {
                    // 'rejected' rows are malformed and would never succeed —
                    // dropping them beats retrying forever; the raw fact is
                    // still in the server logs via the batch response.
                    if let k = r.event_key { markSent(key: k) }
                }
                await MainActor.run {
                    AppState.shared.lastSync = Date()
                    AppState.shared.lastError = nil
                }
            } catch {
                bumpAttempts(keys: pending.map(\.key))
                await MainActor.run { AppState.shared.lastError = "sync: \(error.localizedDescription)" }
            }
        }
        // tracks: 409 = server hasn't derived the trip yet; keep for next flush
        for t in unsentTracks(limit: 5) {
            if let points = try? JSONSerialization.jsonObject(with: Data(t.json.utf8)) as? [[Double]] {
                if let accepted = try? await ServerClient.uploadTrack(startEventKey: t.key, points: points),
                   accepted {
                    markTrackSent(key: t.key)
                }
            }
        }
        await MainActor.run { AppState.shared.refreshOutboxStats() }
    }

    // ── queries ───────────────────────────────────────────────────────────
    private struct PendingEvent {
        let key: String; let kind: String; let place: String?; let source: String
        let confidence: String?; let ts: Int64
        let lat: Double?; let lon: Double?; let acc: Int?
    }

    private func unsentEvents(limit: Int) -> [PendingEvent] {
        var out: [PendingEvent] = []
        var stmt: OpaquePointer?
        let sql = """
            SELECT event_key, kind, place_key, source, confidence, device_ts, lat, lon, acc
            FROM events WHERE sent=0 ORDER BY device_ts LIMIT \(limit)
            """
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }
        while sqlite3_step(stmt) == SQLITE_ROW {
            out.append(PendingEvent(
                key: String(cString: sqlite3_column_text(stmt, 0)),
                kind: String(cString: sqlite3_column_text(stmt, 1)),
                place: sqlite3_column_type(stmt, 2) == SQLITE_NULL ? nil
                    : String(cString: sqlite3_column_text(stmt, 2)),
                source: String(cString: sqlite3_column_text(stmt, 3)),
                confidence: sqlite3_column_type(stmt, 4) == SQLITE_NULL ? nil
                    : String(cString: sqlite3_column_text(stmt, 4)),
                ts: sqlite3_column_int64(stmt, 5),
                lat: sqlite3_column_type(stmt, 6) == SQLITE_NULL ? nil : sqlite3_column_double(stmt, 6),
                lon: sqlite3_column_type(stmt, 7) == SQLITE_NULL ? nil : sqlite3_column_double(stmt, 7),
                acc: sqlite3_column_type(stmt, 8) == SQLITE_NULL ? nil : Int(sqlite3_column_int(stmt, 8))
            ))
        }
        return out
    }

    private struct PendingTrack { let key: String; let json: String }

    private func unsentTracks(limit: Int) -> [PendingTrack] {
        var out: [PendingTrack] = []
        var stmt: OpaquePointer?
        let sql = "SELECT start_event_key, points_json FROM tracks WHERE sent=0 LIMIT \(limit)"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }
        while sqlite3_step(stmt) == SQLITE_ROW {
            out.append(PendingTrack(key: String(cString: sqlite3_column_text(stmt, 0)),
                                    json: String(cString: sqlite3_column_text(stmt, 1))))
        }
        return out
    }

    private func markSent(key: String) {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "UPDATE events SET sent=1 WHERE event_key=?", -1, &stmt, nil) == SQLITE_OK else { return }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_text(stmt, 1, key, -1, SQLITE_TRANSIENT)
        sqlite3_step(stmt)
    }

    private func markTrackSent(key: String) {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "UPDATE tracks SET sent=1 WHERE start_event_key=?", -1, &stmt, nil) == SQLITE_OK else { return }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_text(stmt, 1, key, -1, SQLITE_TRANSIENT)
        sqlite3_step(stmt)
    }

    private func bumpAttempts(keys: [String]) {
        for k in keys {
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, "UPDATE events SET attempts=attempts+1 WHERE event_key=?", -1, &stmt, nil) == SQLITE_OK else { continue }
            sqlite3_bind_text(stmt, 1, k, -1, SQLITE_TRANSIENT)
            sqlite3_step(stmt)
            sqlite3_finalize(stmt)
        }
    }

    // ── UI support ────────────────────────────────────────────────────────
    func stats() -> (pending: Int, sent: Int) {
        open()
        func count(_ sql: String) -> Int {
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return 0 }
            defer { sqlite3_finalize(stmt) }
            return sqlite3_step(stmt) == SQLITE_ROW ? Int(sqlite3_column_int(stmt, 0)) : 0
        }
        return (count("SELECT COUNT(*) FROM events WHERE sent=0"),
                count("SELECT COUNT(*) FROM events WHERE sent=1"))
    }

    struct KindSummary: Identifiable {
        let kind: String
        let total: Int
        let unsent: Int
        let newest: Int64
        var id: String { kind }
    }

    func countsByKind() -> [KindSummary] {
        open()
        var out: [KindSummary] = []
        var stmt: OpaquePointer?
        let sql = """
            SELECT kind, COUNT(*), SUM(CASE WHEN sent=0 THEN 1 ELSE 0 END), MAX(device_ts)
            FROM events GROUP BY kind ORDER BY MAX(device_ts) DESC
            """
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }
        while sqlite3_step(stmt) == SQLITE_ROW {
            out.append(KindSummary(
                kind: String(cString: sqlite3_column_text(stmt, 0)),
                total: Int(sqlite3_column_int(stmt, 1)),
                unsent: Int(sqlite3_column_int(stmt, 2)),
                newest: sqlite3_column_int64(stmt, 3)))
        }
        return out
    }

    func events(kind: String, limit: Int = 100) -> [OutboxEvent] {
        open()
        var out: [OutboxEvent] = []
        var stmt: OpaquePointer?
        let sql = """
            SELECT id, event_key, kind, place_key, device_ts, sent FROM events
            WHERE kind=? ORDER BY device_ts DESC LIMIT \(limit)
            """
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_text(stmt, 1, kind, -1, SQLITE_TRANSIENT)
        while sqlite3_step(stmt) == SQLITE_ROW {
            out.append(OutboxEvent(
                id: sqlite3_column_int64(stmt, 0),
                eventKey: String(cString: sqlite3_column_text(stmt, 1)),
                kind: String(cString: sqlite3_column_text(stmt, 2)),
                placeKey: sqlite3_column_type(stmt, 3) == SQLITE_NULL ? nil
                    : String(cString: sqlite3_column_text(stmt, 3)),
                deviceTS: sqlite3_column_int64(stmt, 4),
                sent: sqlite3_column_int(stmt, 5) == 1))
        }
        return out
    }

    func recent(limit: Int) -> [OutboxEvent] {
        open()
        var out: [OutboxEvent] = []
        var stmt: OpaquePointer?
        let sql = "SELECT id, event_key, kind, place_key, device_ts, sent FROM events ORDER BY device_ts DESC LIMIT \(limit)"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }
        while sqlite3_step(stmt) == SQLITE_ROW {
            out.append(OutboxEvent(
                id: sqlite3_column_int64(stmt, 0),
                eventKey: String(cString: sqlite3_column_text(stmt, 1)),
                kind: String(cString: sqlite3_column_text(stmt, 2)),
                placeKey: sqlite3_column_type(stmt, 3) == SQLITE_NULL ? nil
                    : String(cString: sqlite3_column_text(stmt, 3)),
                deviceTS: sqlite3_column_int64(stmt, 4),
                sent: sqlite3_column_int(stmt, 5) == 1
            ))
        }
        return out
    }
}

// SQLITE_TRANSIENT tells sqlite to copy bound strings before we return.
let SQLITE_TRANSIENT = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
