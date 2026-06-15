import Foundation

// On-device cache of the last successful server fetch. When the reader is unreachable the app serves
// THIS — the reader's real data from the last connection — rather than the synthetic bundled fixture
// (#150). The fixture is only ever used on a cold first run, before any successful fetch.

struct CachedFeed: Codable, Sendable {
    var stories: [Story]
    var sources: [SourceRating]
    var savedAt: Date
}

enum FeedCache {
    private static let fileName = "maat-feed-cache.v1.json"

    private static var fileURL: URL? {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first?
            .appendingPathComponent(fileName)
    }

    static func load() -> CachedFeed? {
        guard let url = fileURL, let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(CachedFeed.self, from: data)
    }

    /// Merge-and-save: update whichever part just succeeded, stamp the time, keep the rest.
    static func update(stories: [Story]? = nil, sources: [SourceRating]? = nil, now: Date = .now) {
        guard let url = fileURL else { return }
        var cached = load() ?? CachedFeed(stories: [], sources: [], savedAt: now)
        if let stories { cached.stories = stories }
        if let sources { cached.sources = sources }
        cached.savedAt = now
        if let data = try? JSONEncoder().encode(cached) {
            try? data.write(to: url, options: .atomic)
        }
    }
}
