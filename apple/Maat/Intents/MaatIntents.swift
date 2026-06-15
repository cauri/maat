import AppIntents
import Foundation

// The app-opening intents (#80). These navigate the running app's UI, so they keep
// `openAppWhenRun = true` and route through `AppRouter` on the in-process `MaatCore` singleton; each
// records an engagement signal (#57). The launch-free, data-returning intents ("top story", "search",
// "add topic", "go deeper") live in the App Intents *extension* so Siri / Shortcuts can run them
// without launching the app (#83) — see Maat/IntentsExtension/.

struct OpenFeedIntent: AppIntent {
    static let title: LocalizedStringResource = "Open Maat Feed"
    static let openAppWhenRun = true

    @MainActor
    func perform() async throws -> some IntentResult {
        MaatCore.shared.router.openFeed()
        MaatCore.shared.analytics.record(.intentInvoked)
        return .result()
    }
}

struct ShowStoryIntent: AppIntent {
    static let title: LocalizedStringResource = "Show a Maat Story"
    static let openAppWhenRun = true

    @Parameter(title: "Story")
    var story: StoryEntity

    @MainActor
    func perform() async throws -> some IntentResult {
        if let full = await MaatCore.shared.story(id: story.id) {
            MaatCore.shared.router.open(full)
        }
        MaatCore.shared.analytics.record(.intentInvoked, storyID: story.id)
        return .result()
    }
}
