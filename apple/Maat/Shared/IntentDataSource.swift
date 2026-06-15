import Foundation

// Extension-safe story access for launch-free App Intents and Spotlight (#83). The app target drives
// the feed through the @MainActor `MaatCore` singleton + `AppRouter`; an App Intents *extension* runs
// out of the app's process and has neither, so the read-only intents reach the same served feed
// through THIS instead — the same `FeedService` (live reader with bundled-fixture fallback) and the
// same on-device `SemanticSearch` the UI uses, with nothing leaving the device (PLAN §6).
//
// It deliberately holds no UI/navigation state: it only *reads* stories so Siri / Shortcuts /
// Spotlight can answer in-place without launching the app.
actor IntentDataSource {
    static let shared = IntentDataSource()

    private let primary: FeedService
    private let fallback: FeedService = FixtureFeedService()

    init() {
        // Same default reader as the app (AppSettings.defaultAPIBaseURL), with the user's override if
        // they set one in the app. Empty / invalid → the bundled fixture, so intents work offline.
        let stored = UserDefaults.standard.string(forKey: "maat.apiBaseURL") ?? AppSettings.defaultAPIBaseURL
        if let url = URL(string: stored), !stored.isEmpty {
            primary = APIFeedService(baseURL: url)
        } else {
            primary = FixtureFeedService()
        }
    }

    /// Stories from the live reader, preferring the last-good on-device cache and finally the bundled
    /// fixture when the reader is unreachable — mirrors `FeedStore.refresh`'s fallback order (#150).
    func stories() async -> [Story] {
        do {
            let fresh = try await primary.loadFeed()
            FeedCache.update(stories: fresh)
            return fresh
        } catch {
            if let cached = FeedCache.load(), !cached.stories.isEmpty {
                return cached.stories
            }
            return (try? await fallback.loadFeed()) ?? []
        }
    }

    func story(id: String) async -> Story? {
        await stories().first { $0.id == id }
    }

    /// Tier-3 "go deeper" (#56), live with the fixture's synthesized expansion as the offline fallback.
    func loadStory(id: String, deeper: Bool) async -> Story? {
        if let live = try? await primary.loadStory(id: id, deeper: deeper) { return live }
        return try? await fallback.loadStory(id: id, deeper: deeper)
    }
}
