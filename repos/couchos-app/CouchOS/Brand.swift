import SwiftUI

/// CouchOS design tokens. docs/BRAND.md is the style manual; this file is
/// its code. The same values drive the web dashboard (web/index.html), so
/// the phone and the TV box look like one product.
///
/// Use these instead of system colours (`.orange`, `.black`, `Color(white:)`).
enum Brand {
    // MARK: colour

    /// The accent: actions, progress, the play button. Never body text.
    static let amber = Color(hex: 0xE5A00D)
    /// Pressed / secondary accent surfaces.
    static let amberDeep = Color(hex: 0xB87E00)
    /// Text and icons placed on amber.
    static let onAmber = Color(hex: 0x101214)

    /// App background (behind everything, edge to edge).
    static let ink = Color(hex: 0x101214)
    /// Cards, the touchpad, artwork placeholders.
    static let panel = Color(hex: 0x1A1D21)
    /// Buttons and controls that sit on a panel or on ink.
    static let raised = Color(hex: 0x23272C)
    /// Hairlines and dividers.
    static let line = Color(hex: 0x2A2E34)

    /// Primary text.
    static let text = Color(hex: 0xE8E6E3)
    /// Secondary text (use `.secondary` in lists; this on custom surfaces).
    static let muted = Color(hex: 0x8B9098)
    /// Hints and glyphs that should almost disappear.
    static let faint = Color(hex: 0x4A4F57)

    static let ok = Color(hex: 0x5FB878)
    static let warn = Color(hex: 0xE0A03A)
    static let bad = Color(hex: 0xE0605A)

    // MARK: shape

    enum Radius {
        /// Artwork, small chips.
        static let small: CGFloat = 10
        /// Buttons, banners, cards.
        static let medium: CGFloat = 16
        /// The touchpad: the one big surface.
        static let large: CGFloat = 28
    }

    // MARK: spacing

    /// Screen edge inset; everything lines up on it.
    static let gutter: CGFloat = 16
}

extension Color {
    /// `Color(hex: 0xE5A00D)`: sRGB, opaque.
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
}

/// The app's version as testers report it: "0.1.0 (12)".
enum AppVersion {
    static var marketing: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?" }
    static var build: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "?" }
    static var display: String { "\(marketing) (\(build))" }
}
