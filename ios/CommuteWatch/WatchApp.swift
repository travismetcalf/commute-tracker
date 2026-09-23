import SwiftUI

@main
struct CommuteWatchApp: App {
    @StateObject private var store = WatchStore.shared

    var body: some Scene {
        WindowGroup {
            WatchContentView()
                .environmentObject(store)
        }
    }
}
