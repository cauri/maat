import Foundation
import Observation

// Single source of truth shared by the UI and by App Intents (#80). App Intents declared in the app
// run in the app's process, so a `@MainActor` singleton lets Siri / Shortcuts / Spotlight drive the
// same `settings` / `topics` / `analytics` / `feed` the UI shows, and route navigation through the
// same `AppRouter`. Everything here is on-device (PLAN §6).

enum AppTab: String, Hashable {
    case feed, search, topics, settings
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

    private var didBootstrap = false

    private init() {}

    /// Load the feed and re-rank against topics. Idempotent — safe to call from the UI's first render
    /// and from any intent that needs data before the app has opened.
    func bootstrap() async {
        feed.setService(settings.makeFeedService())
        await feed.refresh()
        await feed.applyRerank(FoundationModelsReranker(), topics: topics.topics)
        didBootstrap = true
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
