import Foundation

// On-device semantic search over the served feed (#53, PLAN §2.2). Matches the query against each
// story's fact and claims via `Semantics` (embeddings, lexical fallback). Pure on-device; the query
// never leaves the phone.

struct SemanticSearch: Sendable {
    /// Below this best-match score a story is treated as irrelevant and dropped.
    var threshold: Double = 0.08

    func search(_ query: String, in stories: [Story]) -> [Story] {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return stories }
        let scored = stories
            .map { story -> (story: Story, score: Double) in
                let candidates = [story.fact] + story.claims.map(\.text)
                let best = candidates.map { Semantics.similarity(q, $0) }.max() ?? 0
                return (story, best)
            }
            .filter { $0.score >= threshold }
            .sorted { $0.score > $1.score }
        return scored.map(\.story)
    }
}
