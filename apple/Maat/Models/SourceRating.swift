import Foundation

// A news organisation's reputation (BRIEF §6: truthfulness, one scalar §6.2, with a trajectory §6.4).
// Mirrors GET /api/sources. NOTE: the live value is a *provisional proxy* until the reputation fold
// (#37, P3) exists — see the `provisional` flag on the response.

struct SourcesResponse: Codable, Sendable {
    var generatedAt: String?
    var provisional: Bool?
    var note: String?
    var sources: [SourceRating]
}

struct SourceStoryRef: Codable, Sendable, Hashable, Identifiable {
    var id: String
    var fact: String
    var confidence: Double
    var confidencePercent: Int { Int((confidence * 100).rounded()) }
}

struct SourceRating: Codable, Sendable, Identifiable, Hashable {
    var name: String
    var reputation: Double
    var tier: String
    var isPrimary: Bool
    var nStories: Int
    var coldStart: Bool
    var trajectory: [Double]
    var languages: [String]
    /// Present only on the per-source detail (GET /api/source/{name}).
    var stories: [SourceStoryRef]?

    var id: String { name }
    var score: Int { Int((reputation * 100).rounded()) }

    /// The tier, sentence-cased for a headline ("Well-corroborated", "Primary source").
    var displayTier: String {
        guard let first = tier.first else { return tier }
        return String(first).uppercased() + String(tier.dropFirst())
    }
}

extension SourceRating {
    enum Band { case high, medium, low, neutral }

    var band: Band {
        if coldStart { return .neutral }
        if reputation >= 0.75 { return .high }
        if reputation >= 0.5 { return .medium }
        return .low
    }
}
