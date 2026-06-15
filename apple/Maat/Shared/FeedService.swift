import Foundation
import Observation

// How the client gets stories. `APIFeedService` talks to the reader's JSON API (P5 #48, stubbed on
// the FastAPI reader); `FixtureFeedService` reads a bundled corpus-derived snapshot so the app
// builds, previews, and runs with no backend. Both are swapped behind one protocol.

protocol FeedService: Sendable {
    func loadFeed() async throws -> [Story]
    func loadStory(id: String, deeper: Bool) async throws -> Story
    func loadSources() async throws -> [SourceRating]
    func loadSource(name: String) async throws -> SourceRating
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
        // Canonical Feed API (#48, serving/feed.py): de-US ordered, confidence-labelled.
        let (data, resp) = try await session.data(from: baseURL.appending(path: "api/v2/feed"))
        try Self.check(resp)
        return try FeedJSON.decoder.decode(Feed.self, from: data).stories
    }

    func loadStory(id: String, deeper: Bool) async throws -> Story {
        // /api/v2/story attaches the full article bodies the reader opens (deeper is implicit now).
        let url = baseURL.appending(path: "api/v2/story/\(id)")
        let (data, resp) = try await session.data(from: url)
        try Self.check(resp)
        return try FeedJSON.decoder.decode(Story.self, from: data)
    }

    func loadSources() async throws -> [SourceRating] {
        let (data, resp) = try await session.data(from: baseURL.appending(path: "api/sources"))
        try Self.check(resp)
        return try FeedJSON.decoder.decode(SourcesResponse.self, from: data).sources
    }

    func loadSource(name: String) async throws -> SourceRating {
        let (data, resp) = try await session.data(from: baseURL.appending(path: "api/source/\(name)"))
        try Self.check(resp)
        return try FeedJSON.decoder.decode(SourceRating.self, from: data)
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

    func loadSources() async throws -> [SourceRating] {
        try Self.sources()
    }

    func loadSource(name: String) async throws -> SourceRating {
        guard var rating = try Self.sources().first(where: { $0.name == name }) else {
            throw FeedError.notFound(name)
        }
        // Derive the stories this source originated from the bundled feed, so the offline detail links up.
        let stories = (try? Self.feed().stories) ?? []
        rating.stories = stories
            .filter { story in story.originatorGroups.contains { $0.sources.contains(name) } }
            .map { SourceStoryRef(id: $0.id, fact: $0.fact, confidence: $0.confidence) }
        return rating
    }

    static func sources() throws -> [SourceRating] {
        guard let url = Bundle.main.url(forResource: "sources.fixture", withExtension: "json") else {
            throw FeedError.missingFixture
        }
        return try FeedJSON.decoder.decode(SourcesResponse.self, from: Data(contentsOf: url)).sources
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
    /// True when showing the last-good on-device cache because the server was unreachable (#150).
    private(set) var servingCache = false
    private(set) var cacheDate: Date?
    var error: String?

    /// Set by the rerank pass (#53); nil means "server order".
    var rerankedOrder: [String]?

    /// News-organisation reputation ratings (BRIEF §6) — the Sources surface.
    private(set) var sources: [SourceRating] = []

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

    /// Load the news-organisation reputation ratings (BRIEF §6), falling back to the bundled sample.
    func refreshSources() async {
        do {
            sources = try await primary.loadSources()
            FeedCache.update(sources: sources)
        } catch {
            if let cached = FeedCache.load(), !cached.sources.isEmpty {
                sources = cached.sources
            } else {
                sources = (try? await fallback.loadSources()) ?? []
            }
        }
    }

    func loadSource(name: String) async throws -> SourceRating {
        do {
            return try await primary.loadSource(name: name)
        } catch {
            return try await fallback.loadSource(name: name)
        }
    }

    /// The reputation for a source name, if loaded — used to show ratings inline while reading.
    func rating(for name: String) -> SourceRating? {
        sources.first { $0.name == name }
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
            servingCache = false
            cacheDate = nil
            FeedCache.update(stories: stories)
        } catch {
            // Server unreachable. Prefer the last-good cache (real data from the last connection);
            // only a cold first run with no cache falls back to the bundled fixture.
            if let cached = FeedCache.load(), !cached.stories.isEmpty {
                stories = cached.stories
                servingCache = true
                usingFallback = false
                cacheDate = cached.savedAt
                self.error = nil
            } else if let bundled = try? await fallback.loadFeed() {
                stories = bundled
                usingFallback = true
                servingCache = false
                self.error = "Showing the bundled sample — \(error.localizedDescription)"
            } else {
                self.error = error.localizedDescription
            }
        }
    }
}
