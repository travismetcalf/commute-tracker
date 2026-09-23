import SwiftUI

/// Signal's design tokens as native colors — dusk verbatim for dark, the
/// contrast-checked light ramp from the web app. Serif display text uses
/// New York via .fontDesign(.serif): the native idiom of Signal's editorial
/// voice (bundling Source Serif is a later nicety, not a requirement).
struct Theme {
    let paper: Color
    let raised: Color
    let ink: Color
    let inkSoft: Color
    let inkFaint: Color
    let rule: Color
    let accent: Color
    let accentDeep: Color
    let seriesPredicted: Color
    let seriesActual: Color
    let good: Color
    let goodTint: Color
    let bad: Color
    let badTint: Color
    let heat: [Color]

    static let dark = Theme(
        paper: Color(hex: 0x0F1020), raised: Color(hex: 0x1A1B2E),
        ink: Color(hex: 0xECE9F5), inkSoft: Color(hex: 0xB3AECB),
        inkFaint: Color(hex: 0x8D89AA), rule: Color(hex: 0x272844),
        accent: Color(hex: 0xC41E3A), accentDeep: Color(hex: 0xE0485F),
        seriesPredicted: Color(hex: 0xC41E3A), seriesActual: Color(hex: 0x8579DC),
        good: Color(hex: 0x34C759), goodTint: Color(hex: 0x152719),
        bad: Color(hex: 0xEF5348), badTint: Color(hex: 0x241A26),
        heat: [Color(hex: 0x0F890A), Color(hex: 0x65CC85), Color(hex: 0xE4DE27),
               Color(hex: 0xE78530), Color(hex: 0xCB1414)])

    static let light = Theme(
        paper: Color(hex: 0xF4F1EA), raised: .white,
        ink: Color(hex: 0x1A1B2E), inkSoft: Color(hex: 0x4A4763),
        inkFaint: Color(hex: 0x635F7D), rule: Color(hex: 0xE0DBD0),
        accent: Color(hex: 0xA8182F), accentDeep: Color(hex: 0x8E1428),
        seriesPredicted: Color(hex: 0xA8182F), seriesActual: Color(hex: 0x5F54A8),
        good: Color(hex: 0x106B0C), goodTint: Color(hex: 0xE3F0E0),
        bad: Color(hex: 0xCB1414), badTint: Color(hex: 0xF7E9EC),
        heat: [Color(hex: 0x0F890A), Color(hex: 0x65CC85), Color(hex: 0xE4DE27),
               Color(hex: 0xE78530), Color(hex: 0xCB1414)])

    static func current(_ scheme: ColorScheme) -> Theme {
        scheme == .dark ? .dark : .light
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
}

/// Card container matching the web app's raised surfaces.
struct Card<Content: View>: View {
    @Environment(\.colorScheme) private var scheme
    @ViewBuilder let content: Content

    var body: some View {
        let t = Theme.current(scheme)
        VStack(alignment: .leading, spacing: 8) { content }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(t.raised, in: RoundedRectangle(cornerRadius: 13))
            .overlay(RoundedRectangle(cornerRadius: 13).stroke(t.rule, lineWidth: 1))
    }
}
