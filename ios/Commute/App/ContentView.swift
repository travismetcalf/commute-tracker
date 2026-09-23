import SwiftUI

struct ContentView: View {
    @EnvironmentObject var state: AppState
    @State private var tab = 0

    var body: some View {
        TabView(selection: $tab) {
            Tab("Track", systemImage: "car.fill", value: 0) {
                TrackView()
            }
            Tab("Dashboard", systemImage: "chart.bar.fill", value: 1) {
                DashboardView()
            }
            Tab("Settings", systemImage: "gearshape.fill", value: 2) {
                SettingsView()
            }
        }
        // A tapped trip notification lands here with a key to route on;
        // Track (tab 0) is where trips are listed. Cleared right after
        // switching — this is a one-shot deep link, not persistent state,
        // and leaving it set would swallow a second tap on the same trip
        // (onChange fires only on a value transition, not a re-set to an
        // identical value).
        .onChange(of: state.pendingTripKey) { _, key in
            guard key != nil else { return }
            tab = 0
            state.pendingTripKey = nil
        }
        // Horizontal swipes hop between tabs. Simultaneous + direction-gated so
        // vertical scrolling in the lists/dashboard is never intercepted.
        .simultaneousGesture(
            DragGesture(minimumDistance: 30)
                .onEnded { g in
                    let dx = g.translation.width
                    let dy = g.translation.height
                    guard abs(dx) > 60, abs(dx) > 2 * abs(dy) else { return }
                    withAnimation {
                        if dx < 0 { tab = min(tab + 1, 2) }   // swipe left → next
                        else { tab = max(tab - 1, 0) }         // swipe right → previous
                    }
                }
        )
    }
}
