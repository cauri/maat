import SwiftUI

// Mirrors the web reader's visual language (python/maat/web/app.py) so the two clients read as one
// product: paper background, the confidence traffic-light (green/amber/red), and the chip palette for
// voice / fact / projection / synthesis / headline / extremity.

extension Color {
    init(hex: UInt32) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xff) / 255,
            green: Double((hex >> 8) & 0xff) / 255,
            blue: Double(hex & 0xff) / 255,
            opacity: 1
        )
    }
}

enum Palette {
    static let bg = Color(hex: 0xfaf9f7)
    static let card = Color(hex: 0xffffff)
    static let ink = Color(hex: 0x1c1b19)
    static let muted = Color(hex: 0x7a7770)
    static let line = Color(hex: 0xece9e3)

    static let confHigh = Color(hex: 0x3b6d11)
    static let confMid = Color(hex: 0x92580a)
    static let confLow = Color(hex: 0xb3402e)

    static let wireBg = Color(hex: 0xfaeeda)
    static let indepBg = Color(hex: 0xeaf3de)
}

/// A pill style: background + foreground, matching the web reader's `.b` chips.
struct ChipStyle: Sendable {
    var bg: Color
    var fg: Color

    static let own = ChipStyle(bg: Color(hex: 0xf0efe9), fg: Color(hex: 0x67645d))
    static let attributed = ChipStyle(bg: Color(hex: 0xe6f1fb), fg: Color(hex: 0x175fa5))
    static let fact = ChipStyle(bg: Color(hex: 0xeaf3de), fg: Color(hex: 0x3b6d11))
    static let projection = ChipStyle(bg: Color(hex: 0xfaeeda), fg: Color(hex: 0x92580a))
    static let synthesis = ChipStyle(bg: Color(hex: 0xeeedfe), fg: Color(hex: 0x4a3fb0))
    static let headline = ChipStyle(bg: Color(hex: 0x1c1b19), fg: .white)
    static let neutral = ChipStyle(bg: Color(hex: 0xf0efe9), fg: Color(hex: 0x67645d))
    static let extraordinary = ChipStyle(bg: Color(hex: 0xfbe4df), fg: Color(hex: 0xb3402e))
}

extension Story.ConfidenceLevel {
    var color: Color {
        switch self {
        case .high: return Palette.confHigh
        case .medium: return Palette.confMid
        case .low: return Palette.confLow
        }
    }
}

extension Voice {
    var chip: ChipStyle { self == .attributed ? .attributed : .own }
    var label: String { self == .attributed ? "said" : "own voice" }
}

extension Extremity {
    var chip: ChipStyle { self == .extraordinary ? .extraordinary : .neutral }
    var label: String {
        switch self {
        case .extraordinary: return "extraordinary · bar raised"
        case .notable: return "notable claim"
        case .mundane: return "mundane claim"
        }
    }
}
