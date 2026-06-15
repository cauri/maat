import Foundation

// The read model the client consumes from GET /api/feed. The *story* — a corroboration cluster
// (§5.5) — is the unit: its confidence read (§5.6–5.7), its independent-originator collapse, and the
// claims that compose it. These mirror the Postgres projections exactly; JSON is snake_case and
// decoded with `.convertFromSnakeCase` (see FeedService).

struct Feed: Codable, Sendable {
    var generatedAt: String?
    var count: Int?
    var stories: [Story]
}

struct Story: Codable, Sendable, Identifiable, Hashable {
    var id: String
    var fact: String
    var confidence: Double
    var extremity: Extremity
    var independentOriginators: Int
    var hasPrimary: Bool
    var sourceCount: Int
    var languages: [String]
    var originatorGroups: [OriginatorGroup]
    var claims: [Claim]
    /// The full articles behind the story — what the reader reads. Present on the detail payload.
    var articles: [Article]?
    var deeper: Deeper?

    /// Dominant language of the story's claims — drives the on-device translate affordance (#54).
    var primaryLanguage: String { languages.first ?? claims.first?.language ?? "en" }
}

/// A full article — a provenance envelope (BRIEF §8.3) the reader reads in its entirety.
struct Article: Codable, Sendable, Identifiable, Hashable {
    var id: String
    var source: String?
    var title: String?
    var body: String
    var url: String?
    var language: String
    var ingestedAt: String?
}

struct OriginatorGroup: Codable, Sendable, Hashable {
    /// Source display names that collapsed into this single independent originator.
    var sources: [String]
    /// True when more than one outlet collapsed here — i.e. wire syndication / citation cascade.
    var collapsed: Bool
}

struct Claim: Codable, Sendable, Identifiable, Hashable {
    var id: String
    var text: String
    var voice: Voice
    var speaker: String?
    var kind: ClaimKind?
    var isSynthesis: Bool
    var horizon: String?
    var inHeadline: Bool
    var evidenceSpan: String?
    var articleId: String
    var source: String?
    var language: String
}

/// Tier-3 "go deeper" payload (#56) — the expanded provenance the server/PCC pass assembles.
struct Deeper: Codable, Sendable, Hashable {
    var note: String
    var provenance: [Provenance]
}

struct Provenance: Codable, Sendable, Hashable, Identifiable {
    var claimId: String
    var voice: Voice
    var speaker: String?
    var evidenceSpan: String?
    var source: String?
    var id: String { claimId }
}

// MARK: - Lenient enums (unknown values degrade to a safe default rather than failing the decode)

enum Voice: String, Codable, Sendable {
    case own, attributed
    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = Voice(rawValue: raw) ?? .own
    }
}

enum ClaimKind: String, Codable, Sendable {
    case fact, projection
    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = ClaimKind(rawValue: raw) ?? .fact
    }
}

enum Extremity: String, Codable, Sendable {
    case mundane, notable, extraordinary
    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = Extremity(rawValue: raw) ?? .notable
    }
}

// MARK: - Presentation-facing derivations (pure; no UIKit/SwiftUI here)

extension Story {
    enum ConfidenceLevel { case low, medium, high }

    var confidenceLevel: ConfidenceLevel {
        if confidence >= 0.8 { return .high }
        if confidence >= 0.5 { return .medium }
        return .low
    }

    var confidencePercent: Int { Int((confidence * 100).rounded()) }

    /// The corroboration headline shown under the confidence bar.
    var corroborationSummary: String {
        let s = sourceCount == 1 ? "source" : "sources"
        let o = independentOriginators == 1 ? "independent originator" : "independent originators"
        return "\(sourceCount) \(s) → \(independentOriginators) \(o)"
    }
}
