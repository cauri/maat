import AppIntents
import Foundation

// The launch-free intents (#83). These return data (or a spoken answer) without ever opening the app,
// so they live in the App Intents *extension* and set `openAppWhenRun = false`. They read the served
// feed through `IntentDataSource` (live reader + on-device fixture fallback) and reuse the same
// on-device `SemanticSearch` / `Summarizer` the app does — nothing leaves the device (PLAN §6).
//
// Engagement is counted via `IntentAnalytics` (Lane 2, the anonymised aggregate the app and extension
// share through UserDefaults); the id-bearing Lane 1 buffer stays in the app process only.

struct SearchStoriesIntent: AppIntent {
    static let title: LocalizedStringResource = "Search Maat"
    static let openAppWhenRun = false

    @Parameter(title: "Query", requestValueDialog: "What do you want to search Maat for?")
    var query: String

    func perform() async throws -> some IntentResult & ReturnsValue<[StoryEntity]> & ProvidesDialog {
        let results = SemanticSearch()
            .search(query, in: await IntentDataSource.shared.stories())
            .map(StoryEntity.init)
        IntentAnalytics.record(.searched)
        let dialog: IntentDialog = results.isEmpty
            ? "No Maat stories match \(query)."
            : "Found \(results.count) \(results.count == 1 ? "story" : "stories") for \(query)."
        return .result(value: results, dialog: dialog)
    }
}

struct TopStoryIntent: AppIntent {
    static let title: LocalizedStringResource = "Top Story on Maat"
    static let openAppWhenRun = false

    func perform() async throws -> some IntentResult & ReturnsValue<StoryEntity?> & ProvidesDialog {
        IntentAnalytics.record(.intentInvoked)
        guard let top = await IntentDataSource.shared.stories().first else {
            return .result(value: nil, dialog: "There are no Maat stories yet.")
        }
        let summary = await FoundationModelsSummarizer().summarize(top)
        return .result(
            value: StoryEntity(top),
            dialog: "Top story, \(top.confidenceWord.lowercased()). \(summary)"
        )
    }
}

struct AddTopicIntent: AppIntent {
    static let title: LocalizedStringResource = "Add a Maat Topic"
    static let openAppWhenRun = false

    @Parameter(title: "Topic", requestValueDialog: "Which topic should I add?")
    var topic: String

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let added = IntentTopics.add(topic)
        IntentAnalytics.record(.intentInvoked)
        let dialog: IntentDialog = added
            ? "Added \(topic) to your Maat topics — your feed will re-rank next time you open Maat."
            : "\(topic) is already one of your Maat topics."
        return .result(dialog: dialog)
    }
}

struct GoDeeperIntent: AppIntent {
    static let title: LocalizedStringResource = "Go Deeper on a Maat Story"
    static let openAppWhenRun = false

    @Parameter(title: "Story")
    var story: StoryEntity

    func perform() async throws -> some IntentResult & ProvidesDialog {
        IntentAnalytics.record(.wentDeeper)
        let expanded = await IntentDataSource.shared.loadStory(id: story.id, deeper: true)
        let count = expanded?.deeper?.provenance.count ?? 0
        return .result(dialog: "Pulled \(count) source\(count == 1 ? "" : "s") of provenance for this story.")
    }
}
