import SwiftUI

/// Full-screen app icon picker, reached from Appearance. Six 56pt tiles in a
/// `LazyVGrid` used to consume a screenful mid-`Form`; this gets them their
/// own screen. Takes no arguments — it reads and writes `state.appIcon`
/// directly, exactly as the inline picker in `SettingsView` did before this
/// extraction.
struct IconPickerView: View {
    @EnvironmentObject var state: AppState
    @Environment(\.colorScheme) private var scheme

    /// `nil` = the primary AppIcon; the others must exist as alternate
    /// .appiconset entries (see project.yml
    /// ASSETCATALOG_COMPILER_ALTERNATE_APPICON_NAMES).
    private struct IconChoice: Identifiable {
        let id: String        // asset preview image name in the catalog
        let alternateName: String?
        let label: String
    }

    private static let iconChoices: [IconChoice] = [
        IconChoice(id: "IconPreviewBadge", alternateName: nil, label: "Badge"),
        IconChoice(id: "IconPreviewPeaks", alternateName: "AppIconPeaks", label: "Twin Peaks"),
        IconChoice(id: "IconPreviewArc", alternateName: "AppIconArc", label: "The Trip"),
        IconChoice(id: "IconPreviewDart", alternateName: "AppIconDart", label: "Dart"),
        IconChoice(id: "IconPreviewABDusk", alternateName: "AppIconABDusk", label: "A → B"),
        IconChoice(id: "IconPreviewABCrimson", alternateName: "AppIconABCrimson", label: "A → B Red"),
    ]

    /// The current icon's display name, for the summary Appearance shows
    /// next to the `NavigationLink` into this screen. Falls back to the
    /// primary icon's label for any name this build doesn't recognize
    /// (e.g. a stale alternate from a previous build of the icon catalog).
    static func label(for alternateName: String?) -> String {
        iconChoices.first(where: { $0.alternateName == alternateName })?.label ?? "Badge"
    }

    var body: some View {
        let t = Theme.current(scheme)
        Form {
            Section {
                iconGrid
            } footer: {
                Text("Some icons need a moment to appear after switching — that's iOS re-rendering the Home Screen, not a failed change.")
            }
            .listRowBackground(t.raised)
        }
        .scrollContentBackground(.hidden)
        .background(t.paper)
        .navigationTitle("App Icon")
        .navigationBarTitleDisplayMode(.inline)
    }

    private var iconGrid: some View {
        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 14),
                                 count: 3), spacing: 14) {
            ForEach(Self.iconChoices) { choice in
                let selected = state.appIcon == choice.alternateName
                VStack(spacing: 5) {
                    Image(choice.id)
                        .resizable()
                        .frame(width: 56, height: 56)
                        .clipShape(RoundedRectangle(cornerRadius: 12.5, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 12.5, style: .continuous)
                            .strokeBorder(selected ? Color.accentColor : .clear, lineWidth: 3))
                    Text(choice.label)
                        .font(.caption2)
                        .foregroundStyle(selected ? .primary : .secondary)
                }
                .onTapGesture {
                    guard UIApplication.shared.supportsAlternateIcons else { return }
                    state.appIcon = choice.alternateName
                    UIApplication.shared.setAlternateIconName(choice.alternateName) { err in
                        // Revert the highlight if iOS refused the switch.
                        if err != nil {
                            DispatchQueue.main.async {
                                state.appIcon = UIApplication.shared.alternateIconName
                            }
                        }
                    }
                }
            }
        }
        .padding(.vertical, 4)
    }
}
