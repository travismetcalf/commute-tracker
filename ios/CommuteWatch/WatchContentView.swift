import SwiftUI

/// The whole watch app on one crown-scrollable screen: the live number, the
/// two anchors, and the two assert buttons. Dusk-tinted to match the family.
struct WatchContentView: View {
    @EnvironmentObject var store: WatchStore
    @State private var flash: String?

    private let paper = Color(red: 0.059, green: 0.063, blue: 0.125)
    private let ink = Color(red: 0.925, green: 0.914, blue: 0.961)
    private let faint = Color(red: 0.553, green: 0.537, blue: 0.667)
    private let crimson = Color(red: 0.769, green: 0.118, blue: 0.227)
    private let gold = Color(red: 0.847, green: 0.698, blue: 0.353)

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                nowCard
                anchors
                keychainWarning
                buttons
                Text(store.status).font(.footnote).foregroundStyle(faint)
            }
        }
        .background(paper)
        .task { await store.refresh() }
        .refreshable { await store.refresh() }
    }

    private func min(_ sec: Int?) -> String {
        guard let sec else { return "—" }
        return "\(Int((Double(sec) / 60).rounded()))"
    }

    private var nowCard: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(store.summary?.direction == "outbound" ? "TO OFFICE NOW" : "HOME NOW")
                .font(.footnote.weight(.semibold)).foregroundStyle(faint).kerning(0.6)
            HStack(alignment: .firstTextBaseline, spacing: 3) {
                Text(min(store.summary?.now?.travel_sec))
                    .font(.system(size: 44, weight: .bold, design: .serif))
                    .foregroundStyle(ink)
                Text("min").font(.body).foregroundStyle(faint)
            }
            if let live = store.summary?.now?.travel_sec,
               let typ = store.summary?.now?.historic_sec {
                let d = Int((Double(live - typ) / 60).rounded())
                Text(d > 2 ? "+\(d) vs usual" : d < -2 ? "\(-d) quicker" : "about usual")
                    .font(.footnote)
                    .foregroundStyle(d > 2 ? crimson : d < -2 ? .green : faint)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var anchors: some View {
        HStack {
            VStack(alignment: .leading) {
                Text("8 AM").font(.footnote).foregroundStyle(gold)
                Text("\(min(store.summary?.am_out?.avg_sec)) min").font(.headline).foregroundStyle(ink)
            }
            Spacer()
            VStack(alignment: .leading) {
                Text("5 PM").font(.footnote).foregroundStyle(gold)
                Text("\(min(store.summary?.pm_in?.avg_sec)) min").font(.headline).foregroundStyle(ink)
            }
        }
        .padding(.vertical, 4)
    }

    /// Sits directly above the buttons it qualifies. Crimson when the token
    /// landed nowhere — `configured` is false and disabled buttons with no
    /// reason given read as a broken app rather than an unsigned build. Gold
    /// when it landed app-privately: everything here works, but the
    /// complication reads the shared group and won't see it.
    @ViewBuilder private var keychainWarning: some View {
        if let notice = store.keychainNotice, let text = notice.watchNote {
            Label(text, systemImage: notice.isFailure
                    ? "exclamationmark.triangle.fill" : "info.circle.fill")
                .font(.footnote)
                .foregroundStyle(notice.isFailure ? crimson : gold)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var buttons: some View {
        VStack(spacing: 6) {
            assertButton("Drive started", icon: "car.fill", kind: "intent_assert")
            assertButton("Arrived", icon: "flag.checkered", kind: "drive_end")
        }
    }

    private func assertButton(_ label: String, icon: String, kind: String) -> some View {
        Button {
            flash = label
            Task {
                let ok = await WatchAPI.assert(kind: kind)
                await MainActor.run { store.status = ok ? "\(label) ✓" : "send failed" }
                flash = nil
            }
        } label: {
            Label(flash == label ? "Sending…" : label, systemImage: icon)
                .frame(maxWidth: .infinity)
        }
        .tint(crimson)
        .disabled(!store.configured)
    }
}
