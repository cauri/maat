import Foundation

#if canImport(FoundationModels)
import FoundationModels
#endif

// Re-rank the *served* feed against the reader's natural-language topics, on-device (#53, PLAN §2.2).
// This is relevance only — it never touches a story's confidence or truth, and topics never leave the
// phone. Foundation Models when available; embedding similarity otherwise. Returns story ids in order.

protocol Reranker: Sendable {
    func rerank(_ stories: [Story], topics: [String]) async -> [String]
}

/// Deterministic, always-available baseline: score each story by its best topic similarity.
struct EmbeddingReranker: Reranker {
    func rerank(_ stories: [Story], topics: [String]) async -> [String] {
        guard !topics.isEmpty, !stories.isEmpty else { return stories.map(\.id) }
        let scored = stories.enumerated().map { index, story -> (id: String, score: Double, idx: Int) in
            let text = ([story.fact] + story.claims.map(\.text)).joined(separator: " ")
            let best = topics.map { Semantics.similarity($0, text) }.max() ?? 0
            return (story.id, best, index)
        }
        // Sort by relevance, stable on the server's order for ties.
        return scored
            .sorted { $0.score != $1.score ? $0.score > $1.score : $0.idx < $1.idx }
            .map(\.id)
    }
}

/// Foundation Models re-rank; falls back to `EmbeddingReranker` on any unavailability or error.
struct FoundationModelsReranker: Reranker {
    let fallback = EmbeddingReranker()

    func rerank(_ stories: [Story], topics: [String]) async -> [String] {
        guard !topics.isEmpty, !stories.isEmpty else { return stories.map(\.id) }
        #if canImport(FoundationModels)
        if #available(iOS 26, macOS 26, *), Intelligence.isAvailable {
            if let ordered = try? await modelRank(stories, topics: topics) {
                return reconcile(ordered, with: stories, fallback: await fallback.rerank(stories, topics: topics))
            }
        }
        #endif
        return await fallback.rerank(stories, topics: topics)
    }

    /// Keep only known ids in the model's order, then append anything it dropped (in fallback order).
    private func reconcile(_ ordered: [String], with stories: [Story], fallback: [String]) -> [String] {
        let known = Set(stories.map(\.id))
        var seen = Set<String>()
        var result = ordered.filter { known.contains($0) && seen.insert($0).inserted }
        for id in fallback where !seen.contains(id) {
            result.append(id)
            seen.insert(id)
        }
        return result
    }

    #if canImport(FoundationModels)
    @available(iOS 26, macOS 26, *)
    private func modelRank(_ stories: [Story], topics: [String]) async throws -> [String] {
        // DRAFT — review with cauri (in-platform agent prompt fed to Foundation Models; see D22/D23).
        let session = LanguageModelSession(
            instructions: """
            You re-rank a personal news feed for one reader against their topics of interest.
            Judge relevance only — never judge whether a story is true, and never drop a story.
            """
        )
        let lines = stories.map { "\($0.id): \($0.fact)" }.joined(separator: "\n")
        let prompt = """
        Reader topics: \(topics.joined(separator: ", "))

        Stories (id: claim):
        \(lines)

        Order every id from most to least relevant to the reader's topics.
        """
        let response = try await session.respond(to: prompt, generating: RankedFeed.self)
        return response.content.orderedIDs
    }
    #endif
}

#if canImport(FoundationModels)
@available(iOS 26, macOS 26, *)
@Generable
struct RankedFeed {
    @Guide(description: "Every story id, ordered most to least relevant to the reader's topics. Each id appears exactly once.")
    var orderedIDs: [String]
}
#endif
