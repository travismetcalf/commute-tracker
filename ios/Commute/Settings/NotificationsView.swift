import SwiftUI

/// The Notifications screen: every push/email switch the server knows about,
/// rendered as a matrix from `/api/v1/config`'s `notifications` array
/// (`notify.payload()` server-side) — never a hardcoded copy of the catalog,
/// which is exactly the duplication that payload exists to prevent. Also
/// absorbs the anomaly alert-window controls that used to live in their own
/// screen (`AlertSettingsView`), since "when may an alert fire" belongs next
/// to "which alerts are on."
///
/// Each notification switch writes to the server the moment it's toggled,
/// using the `settingKey` the server supplied on that switch — never
/// reconstructed client-side. The alert windows keep the batch "Save to
/// server" shape they had before: five related values edited together, saved
/// together.
struct NotificationsView: View {
    @EnvironmentObject var state: AppState
    @Environment(\.colorScheme) private var scheme

    /// Distinct from "loaded with placeholder values that look real." Nothing
    /// in this screen is shown — and nothing can be saved — until a fetch
    /// actually succeeds. (AlertSettingsView shipped the opposite: hardcoded
    /// defaults visible immediately, a failed fetch silently leaving them in
    /// place, and a Save from that state overwriting the server's real
    /// windows with placeholders. That bug is fixed there too, but here the
    /// gate is structural — the controls don't exist until `.loaded`.)
    private enum LoadState: Equatable { case loading, loaded, failed(String) }
    @State private var loadState: LoadState = .loading

    // Notification switch matrix, keyed by the server-supplied setting_key.
    @State private var notifications: [ServerClient.NotificationType] = []
    @State private var switchValues: [String: Bool] = [:]
    @State private var switchStatus: [String: String] = [:]

    // Alert windows, absorbed from AlertSettingsView.
    @State private var days: Set<Int> = [0, 1, 2, 3, 4]
    @State private var amStart = Self.date(6, 30)
    @State private var amEnd = Self.date(10, 0)
    @State private var pmStart = Self.date(15, 30)
    @State private var pmEnd = Self.date(19, 0)
    @State private var alertStatus = ""

    private static let dayNames = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    var body: some View {
        let t = Theme.current(scheme)
        Form {
            switch loadState {
            case .loading:
                Section {
                    HStack { Spacer(); ProgressView(); Spacer() }
                }
                .listRowBackground(t.raised)
            case .failed(let message):
                Section {
                    Text("Couldn't load notification settings: \(message)")
                        .font(.footnote).foregroundStyle(t.bad)
                } footer: {
                    Text("Showing nothing rather than guessed values. Leave and reopen this screen to retry.")
                }
                .listRowBackground(t.raised)
            case .loaded:
                notificationsSection(t)
                daysSection(t)
                windowSection(t, title: "Morning window", start: $amStart, end: $amEnd)
                windowSection(t, title: "Evening window", start: $pmStart, end: $pmEnd)
                alertSaveSection(t)
            }
        }
        .scrollContentBackground(.hidden)
        .background(t.paper)
        .tint(t.accent)
        .navigationTitle("Notifications")
        .task { await load() }
        .task { await state.refreshNotificationAuth() }
    }

    // MARK: - Notification switch matrix

    /// F2: a push switch showing "on" while iOS has denied delivery for this
    /// app is a lie — Task 8 caught that for the Settings Status row but not
    /// here, which is the more visible place (every push toggle lives on
    /// this screen). The fix does NOT force the switch to "off": the
    /// setting is a genuine server-side preference (`notify_{type}_push`)
    /// that stays meaningfully set — and re-takes effect the moment the user
    /// re-allows notifications — even while iOS is refusing delivery right
    /// now. Showing it as off would be a different lie (claiming the user
    /// chose "off" when they didn't), and disabling the control would block
    /// a legitimate action (setting the preference in advance of granting
    /// permission) for no benefit, since toggling never delivers anything
    /// immediately regardless of permission. So the switch keeps reporting
    /// the true stored value and stays interactive; what changes is that a
    /// persistent, visible affordance says delivery is blocked right now,
    /// with a direct path to fix it — so "I turned this off" and "iOS is
    /// blocking this" can never be mistaken for each other.
    private var pushBlocked: Bool { state.notificationAuth == .denied }

    private func notificationsSection(_ t: Theme) -> some View {
        Section {
            if pushBlocked && notifications.contains(where: { $0.switches.contains { $0.channel == "push" } }) {
                pushBlockedBanner(t)
            }
            ForEach(notifications, id: \.key) { n in
                VStack(alignment: .leading, spacing: 6) {
                    Text(n.label).font(.body.weight(.semibold))
                    if !n.help.isEmpty {
                        Text(n.help).font(.caption).foregroundStyle(.secondary)
                    }
                    ForEach(n.switches, id: \.settingKey) { sw in
                        Toggle(channelLabel(sw.channel), isOn: binding(for: sw.settingKey))
                        if sw.channel == "push" && pushBlocked {
                            Text(switchValues[sw.settingKey] == true
                                 ? "Set to on, but iOS is blocking push for this app — it will not arrive."
                                 : "Off, and iOS is also blocking push for this app right now.")
                                .font(.caption2).foregroundStyle(t.bad)
                        } else if let status = switchStatus[sw.settingKey] {
                            Text(status).font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
                .padding(.vertical, 4)
            }
        } header: {
            Text("Notifications")
        } footer: {
            Text("Each switch saves as soon as you flip it.")
        }
        .listRowBackground(t.raised)
    }

    private func pushBlockedBanner(_ t: Theme) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("iOS is blocking push notifications for this app", systemImage: "bell.slash.fill")
                .font(.caption.weight(.semibold)).foregroundStyle(t.bad)
            Text("Push switches below still show the server's stored preference — turning one on here will not deliver anything until you allow notifications.")
                .font(.caption2).foregroundStyle(.secondary)
            Button("Open system settings") {
                UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!)
            }.font(.caption)
        }
        .padding(.vertical, 4)
    }

    private func channelLabel(_ channel: String) -> String {
        switch channel {
        case "email": "Email"
        case "push": "Push"
        default: channel.capitalized
        }
    }

    private func binding(for settingKey: String) -> Binding<Bool> {
        Binding(
            get: { switchValues[settingKey] ?? false },
            set: { newValue in
                // Captured before the optimistic write, so a failed save has
                // something honest to fall back to if reconciliation (below)
                // can't reach the server either.
                let previous = switchValues[settingKey] ?? false
                switchValues[settingKey] = newValue
                Task { await saveSwitch(settingKey: settingKey, previousValue: previous, value: newValue) }
            })
    }

    /// On failure, this does NOT leave the switch showing the write that
    /// never happened. The web surface gets that guarantee for free —
    /// `hx-swap="outerHTML"` re-renders the row from the server's response,
    /// so what's on screen is always server-rendered truth (Task 4). iOS has
    /// no such swap, so it has to ask for the truth explicitly: re-fetch
    /// `/api/v1/config` and read this switch back from there. (The
    /// `/api/v1/settings` response itself — `{"status", "updated": [keys]}`
    /// — carries no values to reconcile from, only which keys were
    /// accepted, so there's nothing to read out of `saveSettings`'s own
    /// result; a full re-fetch is the only source of truth available.) If
    /// even that fetch fails — still offline — fall back to the value from
    /// before this toggle, which is the best honest answer available rather
    /// than leaving a write that never happened on screen.
    ///
    /// Writing `switchValues` here does NOT re-enter `binding(for:)`'s `set`
    /// closure: SwiftUI calls `set` only when a control writes through the
    /// `Binding` it was handed (i.e. the user operating the Toggle);
    /// mutating the backing `@State` from this async task is a plain
    /// assignment; `set` is not a property observer on it. No second save
    /// gets triggered by this method touching `switchValues`.
    private func saveSwitch(settingKey: String, previousValue: Bool, value: Bool) async {
        switchStatus[settingKey] = "saving…"
        // The server decodes a JSON bool straight into "0"/"1"
        // (main._norm_setting_value) — send the bool, not a stringified one.
        let ok = await ServerClient.saveSettings([settingKey: value])
        if ok {
            switchStatus[settingKey] = nil
            return
        }

        // Don't stomp a newer toggle: if the switch has already moved again
        // since we optimistically set `value`, this stale failure is no
        // longer about what's currently on screen — leave it alone.
        guard switchValues[settingKey] == value else { return }

        if let cfg = try? await ServerClient.fetchConfig(),
           let sw = cfg.notifications?.flatMap({ $0.switches }).first(where: { $0.settingKey == settingKey }) {
            switchValues[settingKey] = sw.enabled
        } else {
            switchValues[settingKey] = previousValue
        }
        switchStatus[settingKey] = "save failed — check connection"
    }

    // MARK: - Alert windows (absorbed from AlertSettingsView)

    private func daysSection(_ t: Theme) -> some View {
        Section {
            HStack(spacing: 6) {
                ForEach(0..<7, id: \.self) { d in
                    let on = days.contains(d)
                    Text(Self.dayNames[d])
                        .font(.caption.weight(.semibold))
                        .frame(maxWidth: .infinity, minHeight: 32)
                        .background(on ? Color.accentColor.opacity(0.25) : t.paper,
                                    in: RoundedRectangle(cornerRadius: 8))
                        .overlay(RoundedRectangle(cornerRadius: 8)
                            .strokeBorder(on ? Color.accentColor : .secondary.opacity(0.3)))
                        .onTapGesture { if on { days.remove(d) } else { days.insert(d) } }
                }
            }
        } header: {
            Text("Alert days")
        } footer: {
            Text("Anomaly pushes only fire on these days. Data collection is unaffected.")
        }
        .listRowBackground(t.raised)
    }

    private func windowSection(_ t: Theme, title: String,
                               start: Binding<Date>, end: Binding<Date>) -> some View {
        Section(title) {
            DatePicker("Start", selection: start, displayedComponents: .hourAndMinute)
            DatePicker("End", selection: end, displayedComponents: .hourAndMinute)
        }
        .listRowBackground(t.raised)
    }

    private func alertSaveSection(_ t: Theme) -> some View {
        Section {
            Button("Save alert windows") { Task { await saveAlerts() } }
                .disabled(days.isEmpty)
            if !alertStatus.isEmpty {
                Text(alertStatus).font(.footnote).foregroundStyle(.secondary)
            }
        } footer: {
            Text("The morning brief and silent widget refreshes are separate — this only scopes the \"commute unusually bad\" alerts.")
        }
        .listRowBackground(t.raised)
    }

    private static func date(_ h: Int, _ m: Int) -> Date {
        Calendar.current.date(from: DateComponents(hour: h, minute: m)) ?? .now
    }
    private static func hhmm(_ d: Date) -> String {
        let c = Calendar.current.dateComponents([.hour, .minute], from: d)
        return String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
    }
    private static func parse(_ s: String?) -> Date? {
        guard let s, s.contains(":") else { return nil }
        let p = s.split(separator: ":").compactMap { Int($0) }
        guard p.count == 2 else { return nil }
        return date(p[0], p[1])
    }

    // MARK: - Load / save

    private func load() async {
        guard case .loading = loadState else { return }
        do {
            let cfg = try await ServerClient.fetchConfig()
            guard let notifs = cfg.notifications, let alerts = cfg.alerts else {
                loadState = .failed("this server is too old to send notification settings")
                return
            }
            notifications = notifs
            switchValues = Dictionary(uniqueKeysWithValues: notifs.flatMap { n in
                n.switches.map { ($0.settingKey, $0.enabled) }
            })
            if let d = alerts["alert_days"] {
                days = Set(d.split(separator: ",").compactMap { Int($0.trimmingCharacters(in: .whitespaces)) })
            }
            amStart = Self.parse(alerts["alert_am_start"]) ?? amStart
            amEnd = Self.parse(alerts["alert_am_end"]) ?? amEnd
            pmStart = Self.parse(alerts["alert_pm_start"]) ?? pmStart
            pmEnd = Self.parse(alerts["alert_pm_end"]) ?? pmEnd
            loadState = .loaded
        } catch {
            loadState = .failed(error.localizedDescription)
        }
    }

    private func saveAlerts() async {
        alertStatus = "saving…"
        let ok = await ServerClient.saveSettings([
            "alert_days": days.sorted().map(String.init).joined(separator: ","),
            "alert_am_start": Self.hhmm(amStart), "alert_am_end": Self.hhmm(amEnd),
            "alert_pm_start": Self.hhmm(pmStart), "alert_pm_end": Self.hhmm(pmEnd),
        ])
        alertStatus = ok ? "saved ✓" : "save failed — check connection"
    }
}
