import Foundation
import Observation

// Single source of truth shared by the UI and by App Intents (#80). App Intents declared in the app
// run in the app's process, so a `@MainActor` singleton lets Siri / Shortcuts / Spotlight drive the
// same `settings` / `topics` / `analytics` / `feed` the UI shows, and route navigation through the
// same `AppRouter`. Everything here is on-device (PLAN §6).

enum AppTab: String, Hashable {
    case feed, sources, search, following
}

@MainActor
@Observable
final class AppRouter {
    var selectedTab: AppTab = .feed
    /// Detail stack for the Feed tab — intents push a story here.
    var feedPath: [Story] = []

    func openFeed() {
        selectedTab = .feed
    }

    func open(_ story: Story) {
        selectedTab = .feed
        feedPath = [story]
    }
}

@MainActor
@Observable
final class MaatCore {
    static let shared = MaatCore()

    let settings = AppSettings()
    let topics = TopicStore()
    let analytics = Analytics()
    let feed = FeedStore(primary: FixtureFeedService())
    let router = AppRouter()
    let pins = PinStore()

    private var didBootstrap = false

    private init() {}

    /// Load the feed + source reputations and re-rank against topics. Idempotent — safe to call from
    /// the UI's first render and from any intent that needs data before the app has opened.
    func bootstrap() async {
        feed.setService(settings.makeFeedService())
        await feed.refresh()
        await feed.applyRerank(FoundationModelsReranker(), topics: topics.topics)
        await feed.refreshSources()
        didBootstrap = true
        // Donate the loaded stories to Spotlight so they're findable system-wide (#83). Best-effort
        // and off the hot path; a Spotlight hit resolves back to `StoryEntity` (an `IndexedEntity`).
        SpotlightDonor.donate(feed.displayStories)
    }

    /// Reputation ratings, loading first if an intent runs before the UI ever did.
    func sources() async -> [SourceRating] {
        if !didBootstrap || feed.sources.isEmpty {
            await bootstrap()
        }
        return feed.sources
    }

    /// Stories in display order, loading them first if an intent runs before the UI ever did.
    func stories() async -> [Story] {
        if !didBootstrap || feed.stories.isEmpty {
            await bootstrap()
        }
        return feed.displayStories
    }

    func story(id: String) async -> Story? {
        await stories().first { $0.id == id }
    }

    func addTopic(_ text: String) async {
        topics.add(text)
        await feed.applyRerank(FoundationModelsReranker(), topics: topics.topics)
    }
}
