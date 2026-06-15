import Foundation

#if canImport(FoundationModels)
import FoundationModels
#endif

// One place to ask "can we run the on-device model right now?" (PLAN §2.2 Tier 2). Apple Intelligence
// is unavailable on the simulator and on ineligible / not-yet-enabled devices, so every Foundation
// Models path degrades to a deterministic fallback. The app must build and run regardless.

enum Intelligence {
    static var isAvailable: Bool {
        #if canImport(FoundationModels)
        if #available(iOS 26, macOS 26, *) {
            if case .available = SystemLanguageModel.default.availability { return true }
        }
        #endif
        return false
    }

    /// Human-readable status for the Settings screen.
    static var statusDescription: String {
        #if canImport(FoundationModels)
        if #available(iOS 26, macOS 26, *) {
            switch SystemLanguageModel.default.availability {
            case .available:
                return "On-device model ready"
            case .unavailable(.deviceNotEligible):
                return "This device isn't eligible for Apple Intelligence — using fallbacks"
            case .unavailable(.appleIntelligenceNotEnabled):
                return "Turn on Apple Intelligence in Settings to enable on-device intelligence"
            case .unavailable(.modelNotReady):
                return "The on-device model is downloading — using fallbacks for now"
            case .unavailable:
                return "On-device model unavailable — using fallbacks"
            }
        }
        #endif
        return "On-device model unavailable on this OS — using fallbacks"
    }
}
