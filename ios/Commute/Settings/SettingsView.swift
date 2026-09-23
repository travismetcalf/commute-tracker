import SwiftUI
import CoreLocation

struct SettingsView: View {
    @EnvironmentObject var state: AppState
    @Environment(\.colorScheme) private var scheme
    @State private var tokenField: String = ""
    /// Only ever assigned from `Keychain.hasToken` — an actual read-back — so
    /// the row cannot claim "stored in Keychain" on the strength of a write
    /// that was refused.
    @State private var tokenStored = Keychain.hasToken
    @State private var tokenNote: String?
    @State private var connection: String = "—"
    /// F3: whether the server's notification catalog currently has at least
    /// one push-only type (a type with no "email" in its `channels` — e.g.
    /// `trip_completed`), read from `/api/v1/config`'s `notifications` array
    /// rather than hardcoded, so a future catalog change can't silently make
    /// this subtitle wrong again. nil until the fetch resolves; the subtitle
    /// stays conservative (no unverified "email still works" claim) until
    /// then — see `notificationSummary`.
    @State private var hasPushOnlyNotificationType: Bool?

    var body: some View {
        let t = Theme.current(scheme)
        NavigationStack {
            Form {
                Group {
                    notificationsSection
                    serverSection
                    trackingSection
                    appearanceSection
                    statusSection
                }
                .listRowBackground(t.raised)
            }
            .scrollContentBackground(.hidden)
            .background(t.paper)
            .navigationTitle("Settings")
            .onAppear {
                state.refreshPermissions()
                state.refreshOutboxStats()
                tokenStored = Keychain.hasToken
                Task { await state.refreshNotificationAuth() }
                Task { await loadPushOnlyFlag() }
            }
        }
    }

    /// First section — Notifications absorbed the alert windows (Task 7) and
    /// is the most-visited screen from here, so it leads rather than sitting
    /// after Server/Tracking as an afterthought.
    private var notificationsSection: some View {
        Section {
            NavigationLink {
                NotificationsView()
            } label: {
                VStack(alignment: .leading, spacing: 2) {
                    Label("Notifications", systemImage: "bell.badge")
                    Text(notificationSummary)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        } header: {
            Text("Notifications")
        } footer: {
            Text("Which alerts fire, where they go, and when.")
        }
    }

    /// Reuses `state.notificationAuth` (also read by the Status row below)
    /// rather than fetching the switch matrix here too — that fetch belongs
    /// to NotificationsView alone, per Task 7's structural load gate. The
    /// push-only check below is a separate, much smaller read (just the
    /// catalog's `channels`, not the live switch values), so it doesn't
    /// reopen that gate — see `loadPushOnlyFlag()`.
    ///
    /// F3: "email alerts still work" is false for a push-only type — there
    /// is no email channel for it to fall back to (`trip_completed` today;
    /// the point of reading `hasPushOnlyNotificationType` from the catalog
    /// instead of naming that type here is that the next push-only type the
    /// catalog gains makes this correct automatically). Until the catalog
    /// fetch resolves, say nothing unverified either way.
    private var notificationSummary: String {
        switch state.notificationAuth {
        case .authorized, .provisional, .ephemeral:
            "Push allowed — manage alerts and windows"
        case .denied:
            switch hasPushOnlyNotificationType {
            case true:
                "Push denied in iOS Settings — push-only alerts won't arrive; others still email"
            case false:
                "Push denied in iOS Settings — email alerts still work"
            case nil:
                "Push denied in iOS Settings — some alerts may not arrive"
            }
        case .notDetermined:
            "Push permission not yet requested"
        @unknown default:
            "Manage alerts and windows"
        }
    }

    /// Reads just the catalog shape (`channels` per notification type) to
    /// answer "does anything here lack an email fallback" — never a
    /// hardcoded type key, so this tracks `notify.CATALOG` automatically.
    /// A fetch failure leaves `hasPushOnlyNotificationType` nil, which
    /// `notificationSummary` already renders as a conservative "some alerts
    /// may not arrive" rather than guessing.
    private func loadPushOnlyFlag() async {
        guard let cfg = try? await ServerClient.fetchConfig(), let notifs = cfg.notifications else { return }
        hasPushOnlyNotificationType = notifs.contains {
            $0.channels.contains("push") && !$0.channels.contains("email")
        }
    }

    private var serverSection: some View {
        Section {
            TextField("Server URL", text: $state.serverURL)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            if tokenStored {
                LabeledContent("API token") {
                    HStack {
                        Text("stored in Keychain").foregroundStyle(.secondary)
                        Button("Remove", role: .destructive) {
                            Keychain.token = nil
                            // Same rule as saving: read back rather than assume.
                            // A refused delete would otherwise leave the row
                            // reporting the token gone while it is still there.
                            tokenStored = Keychain.hasToken
                            if case .failed(let status) = Keychain.lastWrite {
                                tokenNote = "Keychain refused to remove the token "
                                    + "(\(SecStatus.describe(status))). It is still stored."
                            } else {
                                tokenNote = nil
                            }
                        }.font(.footnote)
                    }
                }
            } else {
                SecureField("Bearer token", text: $tokenField)
                Button("Save token") {
                    Keychain.token = tokenField.trimmingCharacters(in: .whitespacesAndNewlines)
                    // Believe the Keychain, not the button. A refused write used
                    // to still flip this to "stored", so the app spent the rest
                    // of the session 401-ing against a token it never had.
                    if case .failed(let status) = Keychain.lastWrite {
                        tokenNote = "Keychain refused the write "
                            + "(\(SecStatus.describe(status))). Nothing was stored."
                        tokenStored = Keychain.hasToken
                        return          // keep tokenField — don't lose the paste
                    }
                    // Confirm by reading it back, not by trusting the status.
                    tokenStored = Keychain.hasToken
                    guard tokenStored else {
                        tokenNote = "The Keychain reported success but the token could "
                            + "not be read back. It has not been stored."
                        return
                    }
                    tokenNote = Keychain.lastWrite == .storedAppOnly
                        ? "Stored, but this build has no shared-keychain entitlement — the "
                          + "widget and watch can't read it. Expected in the Simulator."
                        : nil
                    tokenField = ""
                    connection = "checking…"
                    Task {
                        connection = await ServerClient.checkConnection()
                        await TripEngine.shared.resyncConfig()   // arm geofences NOW
                        await TripEngine.shared.reconcileAndFlush()
                        await WatchSync.shared.push()
                    }
                }
                .disabled(tokenField.count < 32)
            }
            if let tokenNote {
                Text(tokenNote).font(.footnote).foregroundStyle(.secondary)
            }
            Button("Test connection") {
                connection = "checking…"
                Task {
                    connection = await ServerClient.checkConnection()
                    if connection == "connected" {
                        await TripEngine.shared.resyncConfig()
                        await WatchSync.shared.push()
                    }
                }
            }
            LabeledContent("Connection", value: connection)
        } header: {
            Text("Server")
        } footer: {
            Text("The token comes from the server installer — keep it in your password manager. It authenticates everything in this app — no web login needed.")
        }
    }

    private var trackingSection: some View {
        Section {
            Picker("GPS accuracy", selection: Binding(
                get: { state.gpsAccuracy },
                set: { state.gpsAccuracy = $0 })) {
                Text("Coarse — battery-friendly").tag(GPSAccuracy.coarse)
                Text("Precise — full GPS").tag(GPSAccuracy.precise)
            }
            Button("Re-run reconciliation now") {
                Task { await TripEngine.shared.reconcileAndFlush() }
            }
        } header: {
            Text("Tracking")
        } footer: {
            Text("Coarse (~100 m) is served from cell/Wi-Fi and costs almost nothing; Precise runs the GPS continuously during drives (~10–20% battery per commute day). Coarse is enough to tell corridors apart in most cases.")
        }
    }

    private var appearanceSection: some View {
        Section("Appearance") {
            Picker("Theme", selection: Binding(
                get: { state.appearance },
                set: { state.appearance = $0 })) {
                ForEach(Appearance.allCases) { a in
                    Text(a.rawValue.capitalized).tag(a)
                }
            }
            .pickerStyle(.segmented)
            NavigationLink {
                IconPickerView()
            } label: {
                LabeledContent("App icon", value: IconPickerView.label(for: state.appIcon))
            }
        }
    }

    private var statusSection: some View {
        Section("Status") {
            row("Location", ok: state.locationAuth == .authorizedAlways,
                detail: authLabel(state.locationAuth))
            row("Motion", ok: state.motionAuth == .authorized,
                detail: motionLabel)
            row("Notifications", ok: notificationAuthOK,
                detail: notificationAuthLabel)
            LabeledContent("Outbox pending", value: "\(state.pendingEvents)")
            if let sync = state.lastSync {
                LabeledContent("Last sync", value: sync.formatted(.relative(presentation: .named)))
            }
            Button("Open system settings") {
                UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!)
            }
        }
    }

    private func row(_ name: String, ok: Bool, detail: String) -> some View {
        LabeledContent(name) {
            HStack {
                Text(detail).foregroundStyle(.secondary)
                Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                    .foregroundStyle(ok ? .green : .orange)
            }
        }
    }

    private func authLabel(_ s: CLAuthorizationStatus) -> String {
        switch s {
        case .authorizedAlways: "Always"
        case .authorizedWhenInUse: "While Using — upgrade needed"
        case .denied: "Denied"
        case .restricted: "Restricted"
        case .notDetermined: "Not asked yet"
        @unknown default: "Unknown"
        }
    }

    private var motionLabel: String {
        switch state.motionAuth {
        case .authorized: "Allowed"
        case .denied: "Denied"
        case .restricted: "Restricted"
        case .notDetermined: "Not asked yet"
        @unknown default: "Unknown"
        }
    }

    /// `.provisional` (quiet delivery, no prompt shown) and `.ephemeral`
    /// (App Clips) both mean iOS will actually deliver something, so both
    /// count as "ok" for this row same as `.authorized`.
    private var notificationAuthOK: Bool {
        switch state.notificationAuth {
        case .authorized, .provisional, .ephemeral: true
        case .denied, .notDetermined: false
        @unknown default: false
        }
    }

    private var notificationAuthLabel: String {
        switch state.notificationAuth {
        case .authorized: "Allowed"
        case .provisional: "Provisional (quiet delivery)"
        case .ephemeral: "Ephemeral"
        case .denied: "Denied"
        case .notDetermined: "Not asked yet"
        @unknown default: "Unknown"
        }
    }
}
