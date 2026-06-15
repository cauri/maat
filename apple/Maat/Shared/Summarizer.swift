import Foundation

#if canImport(FoundationModels)
import FoundationModels
#endif

// "Summarise-to-taste" on-device (#53, PLAN §2.2). Foundation Models when available; a deterministic
// extractive summary otherwise. The summary is built only from the story's own claims — it must not
// add facts or inflate confidence.

protocol Summarizer: Sendable {
    func summarize(_ story: Story) async -> String
}

struct FoundationModelsSummarizer: Summarizer {
    let fallback = ExtractiveSummarizer()

    func summarize(_ story: Story) async -> String {
        #if canImport(FoundationModels)
        if #available(iOS 26, macOS 26, *), Intelligence.isAvailable {
            if let s = try? await modelSummary(story), !s.isEmpty { return s }
        }
        #endif
        return await fallback.summarize(story)
    }

    #if canImport(FoundationModels)
    @available(iOS 26, macOS 26, *)
    private func modelSummary(_ story: Story) async throws -> String {
        // DRAFT — review with cauri (in-platform agent prompt fed to Foundation Models; see D22/D23).
        let session = LanguageModelSession(
            instructions: """
            You summarise one news story for a reader in at most two sentences.
            Use only the claims given. Do not add facts. Preserve whether each claim is stated in the
            outlet's own voice or attributed to someone, and never overstate how confirmed it is.
            """
        )
        let claims = story.claims.map { c -> String in
            let voice = c.voice == .attributed ? "attributed to \(c.speaker ?? "someone")" : "own voice"
            let kind = c.kind.map { " [\($0.rawValue)]" } ?? ""
            return "- \(c.text) (\(voice))\(kind)"
        }.joined(separator: "\n")
        let prompt = """
        Story: \(story.fact)
        Claims:
        \(claims)
        """
        let response = try await session.respond(to: prompt)
        return response.content.trimmingCharacters(in: .whitespacesAndNewlines)
    }
    #endif
}

struct ExtractiveSummarizer: Summarizer {
    func summarize(_ story: Story) async -> String {
        var parts = [story.fact]
        let extra = story.claims
            .filter { !$0.inHeadline && $0.text != story.fact }
            .prefix(2)
            .map(\.text)
        parts.append(contentsOf: extra)
        return parts.joined(separator: " ")
    }
}
