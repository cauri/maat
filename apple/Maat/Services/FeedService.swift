import Foundation
import Observation

// How the client gets stories. `APIFeedService` talks to the reader's JSON API (P5 #48, stubbed on
// the FastAPI reader); `FixtureFeedService` reads a bundled corpus-derived snapshot so the app
// builds, previews, and runs with no backend. Both are swapped behind one protocol.

protocol FeedService: Sendable {
    func loadFeed() async throws -> [Story]
    func loadStory(id: String, deeper: Bool) async throws -> Story
}

enum FeedError: LocalizedError {
    case badResponse(Int)
    case missingFixture
    case notFound(String)

    var errorDescription: String? {
        switch self {
        case .badResponse(let code): return "The reader returned HTTP \(code)."
        case .missingFixture: return "Bundled sample feed is missing."
        case .notFound(let id): return "No story \(id)."
        }
    }
}

enum FeedJSON {
    static var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }
}

// MARK: - Live API

struct APIFeedService: FeedService {
    var baseURL: URL
    var session: URLSession = .shared

    func loadFeed() async throws -> [Story] {
        let (data, resp) = try await session.data(from: baseURL.appending(path: "api/feed"))
        try Self.check(resp)
        return try FeedJSON.decoder.decode(Feed.self, from: data).stories
    }

    func loadStory(id: String, deeper: Bool) async throws -> Story {
        var url = baseURL.appending(path: "api/story/\(id)")
        if deeper { url.append(queryItems: [URLQueryItem(name: "deeper", value: "1")]) }
        let (data, resp) = try await session.data(from: url)
        try Self.check(resp)
        return try FeedJSON.decoder.decode(Story.self, from: data)
    }

    private static func check(_ resp: URLResponse) throws {
        guard let http = resp as? HTTPURLResponse else { return }
        guard (200..<300).contains(http.statusCode) else { throw FeedError.badResponse(http.statusCode) }
    }
}

// MARK: - Bundled fixture (offline / preview / no-backend default)

struct FixtureFeedService: FeedService {
    func loadFeed() async throws -> [Story] {
        try Self.feed().stories
    }

    func loadStory(id: String, deeper: Bool) async throws -> Story {
        guard var story = try Self.feed().stories.first(where: { $0.id == id }) else {
            throw FeedError.notFound(id)
        }
        if deeper { story.deeper = Self.synthesizeDeeper(for: story) }
        return story
    }

    static func feed() throws -> Feed {
        guard let url = Bundle.main.url(forResource: "feed.fixture", withExtension: "json") else {
            throw FeedError.missingFixture
        }
        return try FeedJSON.decoder.decode(Feed.self, from: Data(contentsOf: url))
    }

    /// Mirrors the backend's deeper stub so offline behaviour matches the live path (#56).
    static func synthesizeDeeper(for story: Story) -> Deeper {
        Deeper(
            note: "Tier-3 expansion (server/PCC stub): primary-source fetch-and-verify and "
                + "cross-language corroboration would run here.",
            provenance: story.claims.map {
                Provenance(claimId: $0.id, voice: $0.voice, speaker: $0.speaker,
                           evidenceSpan: $0.evidenceSpan, source: $0.source)
            }
        )
    }
}

// MARK: - Store

@MainActor
@Observable
final class FeedStore {
    private(set) var stories: [Story] = []
    private(set) var isLoading = false
    private(set) var usingFallback = false
    var error: String?

    /// Set by the rerank pass (#53); nil means "server order".
    var rerankedOrder: [String]?

    private var primary: FeedService
    private let fallback: FeedService

    init(primary: FeedService, fallback: FeedService = FixtureFeedService()) {
        self.primary = primary
        self.fallback = fallback
    }

    /// Repoint at a different source (e.g. the Settings screen toggling fixture ↔ live reader).
    func setService(_ service: FeedService) {
        primary = service
        rerankedOrder = nil
    }

    /// Re-rank the loaded stories against the reader's topics, on-device (#53).
    func applyRerank(_ reranker: Reranker, topics: [String]) async {
        guard !stories.isEmpty else { return }
        rerankedOrder = await reranker.rerank(stories, topics: topics)
    }

    /// Stories in display order — reranked-against-topics when a rerank has run, else server order.
    var displayStories: [Story] {
        guard let order = rerankedOrder else { return stories }
        let rank = Dictionary(uniqueKeysWithValues: order.enumerated().map { ($1, $0) })
        return stories.sorted { (rank[$0.id] ?? .max) < (rank[$1.id] ?? .max) }
    }

    /// Tier-3 "go deeper" (#56): fetch the expanded story (provenance / cross-language corroboration)
    /// from the server/PCC tier, falling back to the bundled sample's synthesized deeper view offline.
    func loadStory(id: String, deeper: Bool) async throws -> Story {
        do {
            return try await primary.loadStory(id: id, deeper: deeper)
        } catch {
            return try await fallback.loadStory(id: id, deeper: deeper)
        }
    }

    func refresh() async {
        isLoading = true
        error = nil
        defer { isLoading = false }
        do {
            stories = try await primary.loadFeed()
            usingFallback = false
        } catch {
            // The reader is unreachable — fall back to the bundled sample rather than an empty feed.
            do {
                stories = try await fallback.loadFeed()
                usingFallback = true
                self.error = "Showing the bundled sample — \(error.localizedDescription)"
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
