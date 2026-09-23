import SwiftUI
import CoreLocation

struct TrackView: View {
    @EnvironmentObject var state: AppState
    @Environment(\.colorScheme) private var scheme
    private let locationManager = CLLocationManager()

    var body: some View {
        let t = Theme.current(scheme)
        NavigationStack {
            List {
                Group {
                    permissionSection
                    statusSection
                    manualSection
                    tripsSection
                    eventsSection
                }
                .listRowBackground(t.raised)
            }
            .scrollContentBackground(.hidden)
            .background(t.paper)
            .navigationTitle("Track")
            .refreshable {
                await TripEngine.shared.reconcileAndFlush()
                state.refreshOutboxStats()
            }
            .onAppear {
                state.refreshPermissions()
                state.refreshOutboxStats()
            }
        }
    }

    // ── permissions: the primary place silent failure becomes visible ──────
    @ViewBuilder private var permissionSection: some View {
        if state.locationAuth != .authorizedAlways || state.motionAuth != .authorized {
            Section("Permissions needed") {
                if state.locationAuth == .notDetermined {
                    Button("Enable location (step 1 of 2)") {
                        // Two-prompt flow: this grants provisional Always; iOS
                        // shows the confirming prompt later, on its own.
                        locationManager.requestAlwaysAuthorization()
                    }
                } else if state.locationAuth != .authorizedAlways {
                    Label("Location must be “Always” for background tracking",
                          systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    Button("Open Settings") {
                        UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!)
                    }
                }
                if state.motionAuth == .notDetermined {
                    Button("Enable motion (confirms drives, repairs gaps)") {
                        Task { await TripEngine.shared.requestMotionPermission() }
                    }
                } else if state.motionAuth != .authorized {
                    Label("Motion access lets Commute repair missed trips",
                          systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    Button("Open Settings") {
                        UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!)
                    }
                }
            }
        }
    }

    private var statusSection: some View {
        Section("Engine") {
            LabeledContent("Status", value: state.engineStatus)
            LabeledContent("Geofences armed", value: "\(state.monitorsArmed)")
            LabeledContent("Recording") {
                Image(systemName: state.isRecording ? "record.circle.fill" : "pause.circle")
                    .foregroundStyle(state.isRecording ? .red : .secondary)
            }
            LabeledContent("Pending upload", value: "\(state.pendingEvents)")
            if let sync = state.lastSync {
                LabeledContent("Last sync", value: sync.formatted(.relative(presentation: .named)))
            }
            if let err = state.lastError {
                Label(err, systemImage: "wifi.exclamationmark")
                    .foregroundStyle(.orange)
                    .font(.footnote)
            }
        }
    }

    // Manual controls double as the App Intent's entry points; useful before
    // the Bluetooth automation exists and for testing.
    private var manualSection: some View {
        Section("Manual") {
            Button {
                Task { await TripEngine.shared.assertDriveStarted() }
            } label: { Label("I'm driving now", systemImage: "car.fill") }
            Button {
                Task { await TripEngine.shared.assertArrived() }
            } label: { Label("I've arrived", systemImage: "flag.checkered") }
        }
    }

    private var tripsSection: some View {
        Section("Trips") {
            NavigationLink { TripsView() } label: {
                Label("Review trips", systemImage: "checkmark.seal")
            }
        }
    }

    private var eventsSection: some View {
        Section {
            if state.kindSummaries.isEmpty {
                Text("No events yet — leave a geofence or use the manual buttons.")
                    .foregroundStyle(.secondary)
                    .font(.footnote)
            }
            ForEach(state.kindSummaries) { s in
                NavigationLink {
                    EventListView(kind: s.kind)
                } label: {
                    HStack(spacing: 12) {
                        Image(systemName: EventKind.icon(s.kind))
                            .foregroundStyle(EventKind.tint(s.kind))
                            .frame(width: 26)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(EventKind.label(s.kind))
                                .font(.subheadline.weight(.semibold))
                            Text("latest \(Date(timeIntervalSince1970: TimeInterval(s.newest)), format: .dateTime.weekday().hour().minute())")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if s.unsent > 0 {
                            Text("\(s.unsent)")
                                .font(.caption.bold()).foregroundStyle(.white)
                                .padding(.horizontal, 7).padding(.vertical, 2)
                                .background(.orange, in: Capsule())
                        }
                        Text("\(s.total)")
                            .font(.subheadline.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
            }
        } header: {
            Text("Events")
        } footer: {
            Text("Grouped by type · orange badge = queued for upload. Tap for details.")
        }
    }
}

/// Human names, icons, and tints for the raw event kinds.
enum EventKind {
    static func label(_ k: String) -> String {
        switch k {
        case "geofence_leave": "Left a place"
        case "geofence_enter": "Arrived at a place"
        case "intent_assert": "Drive asserted"
        case "drive_start": "Drive started"
        case "drive_end": "Drive ended"
        case "motion_segment": "Detected drives"
        case "manual": "Manual entries"
        default: k
        }
    }
    static func icon(_ k: String) -> String {
        switch k {
        case "geofence_leave": "arrow.up.forward.square"
        case "geofence_enter": "arrow.down.left.square"
        case "intent_assert", "drive_start": "car.fill"
        case "drive_end": "flag.checkered"
        case "motion_segment": "waveform.path.ecg"
        case "manual": "pencil"
        default: "circle"
        }
    }
    static func tint(_ k: String) -> Color {
        switch k {
        case "geofence_leave", "drive_start", "intent_assert": .blue
        case "geofence_enter", "drive_end": .green
        case "motion_segment": .purple
        default: .secondary
        }
    }
}

/// Drill-down: every event of one kind, newest first.
struct EventListView: View {
    let kind: String
    @Environment(\.colorScheme) private var scheme
    @State private var events: [OutboxEvent] = []

    var body: some View {
        let t = Theme.current(scheme)
        List {
            Section {
                ForEach(events) { ev in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(Date(timeIntervalSince1970: TimeInterval(ev.deviceTS)),
                                 format: .dateTime.month().day().hour().minute())
                                .font(.subheadline)
                            if let place = ev.placeKey {
                                Text(place).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                        Image(systemName: ev.sent ? "checkmark.icloud" : "icloud.and.arrow.up")
                            .foregroundStyle(ev.sent ? .green : .orange)
                            .accessibilityLabel(ev.sent ? "Uploaded" : "Queued")
                    }
                }
            } footer: {
                Text("\(events.count) shown · newest first")
            }
            .listRowBackground(t.raised)
        }
        .scrollContentBackground(.hidden)
        .background(t.paper)
        .navigationTitle(EventKind.label(kind))
        .navigationBarTitleDisplayMode(.inline)
        .task { events = await Outbox.shared.events(kind: kind) }
        .refreshable { events = await Outbox.shared.events(kind: kind) }
    }
}
