import AppIntents
import Foundation

// The features Siri / Shortcuts / other apps can invoke (#80). Each reuses the on-device services via
// MaatCore and records an engagement signal (#57). UI-opening intents route through AppRouter; the
// read-only ones return a value + spoken dialog so Siri can answer without launching the app.

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

struct SearchStoriesIntent: AppIntent {
    static let title: LocalizedStringResource = "Search Maat"
    static let openAppWhenRun = false

    @Parameter(title: "Query", requestValueDialog: "What do you want to search Maat for?")
    var query: String

    @MainActor
    func perform() async throws -> some IntentResult & ReturnsValue<[StoryEntity]> & ProvidesDialog {
        let results = SemanticSearch()
            .search(query, in: await MaatCore.shared.stories())
            .map(StoryEntity.init)
        MaatCore.shared.analytics.record(.searched)
        let dialog: IntentDialog = results.isEmpty
            ? "No Maat stories match \(query)."
            : "Found \(results.count) \(results.count == 1 ? "story" : "stories") for \(query)."
        return .result(value: results, dialog: dialog)
    }
}

struct TopStoryIntent: AppIntent {
    static let title: LocalizedStringResource = "Top Story on Maat"
    static let openAppWhenRun = false

    @MainActor
    func perform() async throws -> some IntentResult & ReturnsValue<StoryEntity?> & ProvidesDialog {
        MaatCore.shared.analytics.record(.intentInvoked)
        guard let top = await MaatCore.shared.stories().first else {
            return .result(value: nil, dialog: "There are no Maat stories yet.")
        }
        let summary = await FoundationModelsSummarizer().summarize(top)
        return .result(
            value: StoryEntity(top),
            dialog: "Top story, \(top.confidencePercent)% confidence. \(summary)"
        )
    }
}

struct AddTopicIntent: AppIntent {
    static let title: LocalizedStringResource = "Add a Maat Topic"
    static let openAppWhenRun = false

    @Parameter(title: "Topic", requestValueDialog: "Which topic should I add?")
    var topic: String

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        await MaatCore.shared.addTopic(topic)
        MaatCore.shared.analytics.record(.intentInvoked)
        return .result(dialog: "Added \(topic) to your Maat topics and re-ranked your feed.")
    }
}

struct GoDeeperIntent: AppIntent {
    static let title: LocalizedStringResource = "Go Deeper on a Maat Story"
    static let openAppWhenRun = false

    @Parameter(title: "Story")
    var story: StoryEntity

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        MaatCore.shared.analytics.record(.wentDeeper, storyID: story.id)
        let expanded = try? await MaatCore.shared.feed.loadStory(id: story.id, deeper: true)
        let count = expanded?.deeper?.provenance.count ?? 0
        return .result(dialog: "Pulled \(count) source\(count == 1 ? "" : "s") of provenance for this story.")
    }
}
