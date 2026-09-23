import AppIntents

/// Called by a Shortcuts Personal Automation on the car stereo's Bluetooth
/// CONNECT. Pair ArrivedIntent with the DISCONNECT trigger (new in iOS 26);
/// geofence-enter and stationary detection remain the trip-end backstops.
/// This gives second-level precision where a geofence exit is 3–5 min late.
struct DriveStartedIntent: AppIntent {
    static var title: LocalizedStringResource { "Assert Drive Started" }
    static var description: IntentDescription {
        IntentDescription("Tells Commute a drive is starting right now. Wire it to a Personal Automation on your car's Bluetooth connection.")
    }

    func perform() async throws -> some IntentResult {
        await TripEngine.shared.assertDriveStarted()
        return .result()
    }
}

struct ArrivedIntent: AppIntent {
    static var title: LocalizedStringResource { "Assert Arrived" }
    static var description: IntentDescription {
        IntentDescription("Tells Commute the current drive just ended.")
    }

    func perform() async throws -> some IntentResult {
        await TripEngine.shared.assertArrived()
        return .result()
    }
}

// ── query intents: Siri answers, not just automation triggers ─────────────
// These also work from the Apple Watch with no watch app — Siri on the wrist
// runs the phone's App Shortcuts and speaks the dialog back.

/// "How's my commute" — live time for the clock-appropriate direction.
struct CommuteNowIntent: AppIntent {
    static var title: LocalizedStringResource { "Commute Time Now" }
    static var description: IntentDescription {
        IntentDescription("Current drive time with live traffic — outbound before noon, homebound after.")
    }

    func perform() async throws -> some IntentResult & ReturnsValue<Int> & ProvidesDialog {
        let (data, code) = try await ServerClient.raw("/api/v1/summary")
        guard code == 200,
              let p = try? JSONDecoder().decode(SummaryPayload.self, from: data),
              let live = p.now?.travel_sec else {
            return .result(value: 0, dialog: "I couldn't reach the commute server.")
        }
        let mins = Int((Double(live) / 60).rounded())
        let dir = p.direction == "outbound" ? "Home to office" : "Office to home"
        var delta = ""
        if let typ = p.now?.historic_sec {
            let d = Int((Double(live - typ) / 60).rounded())
            delta = d > 2 ? " — \(d) minutes slower than usual"
                  : d < -2 ? " — \(-d) minutes quicker than usual" : " — about usual"
        }
        return .result(value: mins, dialog: "\(dir) is \(mins) minutes right now\(delta).")
    }
}

/// "When should I leave" — today's optimized departure from the plan grid.
struct WhenToLeaveIntent: AppIntent {
    static var title: LocalizedStringResource { "When To Leave" }
    static var description: IntentDescription {
        IntentDescription("Today's best departure time — home in the morning, office in the afternoon.")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let (data, code) = try await ServerClient.raw("/api/v1/dashboard")
        guard code == 200,
              let p = try? JSONDecoder().decode(DashboardPayload.self, from: data) else {
            return .result(dialog: "I couldn't reach the commute server.")
        }
        let now = Date()
        let cal = Calendar.current
        let dow = (cal.component(.weekday, from: now) + 5) % 7   // 0=Mon
        guard dow < 5, let day = p.plan.days.first(where: { $0.dow == dow }) else {
            return .result(dialog: "No plan for today — it's not a weekday I have data for.")
        }
        let morning = cal.component(.hour, from: now) < 12
        let mod = morning ? day.leave_home_mod : day.leave_office_mod
        let drive = morning ? day.out_sec : day.in_sec
        // Spoken aloud, so it uses the place's own name: the leg you're about
        // to drive STARTS where the other one ended. Falls back to plain
        // English when the server hasn't named its corridors.
        let leg = (p.primary?[morning ? "outbound" : "inbound"] ?? nil)
        let place = leg?.origin_label ?? (morning ? "home" : "the office")
        let pick = day.pick ? "" : " Heads up: today isn't one of your recommended office days."
        return .result(dialog:
            "Leave \(place) around \(Fmt.hhmm(mod)) — the drive should take about \(Int((Double(drive)/60).rounded())) minutes.\(pick)")
    }
}

struct CommuteShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: DriveStartedIntent(),
                    phrases: ["Start a drive in \(.applicationName)"],
                    shortTitle: "Drive started",
                    systemImageName: "car.fill")
        AppShortcut(intent: ArrivedIntent(),
                    phrases: ["End the drive in \(.applicationName)"],
                    shortTitle: "Arrived",
                    systemImageName: "flag.checkered")
        AppShortcut(intent: CommuteNowIntent(),
                    phrases: ["How's my commute in \(.applicationName)",
                              "Commute time in \(.applicationName)"],
                    shortTitle: "Commute now",
                    systemImageName: "clock.fill")
        AppShortcut(intent: WhenToLeaveIntent(),
                    phrases: ["When should I leave in \(.applicationName)",
                              "When to leave in \(.applicationName)"],
                    shortTitle: "When to leave",
                    systemImageName: "arrow.right.circle.fill")
    }
}
