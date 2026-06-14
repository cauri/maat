import AppIntents
import Foundation

// A story exposed to Siri / Shortcuts / Spotlight (#80). Backed by the same feed the UI shows
// (via MaatCore); the string query reuses on-device semantic search so "search Maat for …" works
// without the query ever leaving the phone.

struct StoryEntity: AppEntity, Identifiable {
    static let typeDisplayRepresentation = TypeDisplayRepresentation(name: "Story")
    static let defaultQuery = StoryEntityQuery()

    let id: String
    let fact: String
    let confidencePercent: Int
    let independentOriginators: Int
    let hasPrimary: Bool

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(
            title: "\(fact)",
            subtitle: "\(confidencePercent)% confidence · \(independentOriginators) independent originators"
        )
    }

    init(_ story: Story) {
        id = story.id
        fact = story.fact
        confidencePercent = story.confidencePercent
        independentOriginators = story.independentOriginators
        hasPrimary = story.hasPrimary
    }
}

struct StoryEntityQuery: EntityQuery {
    @MainActor
    func entities(for identifiers: [String]) async throws -> [StoryEntity] {
        let wanted = Set(identifiers)
        return await MaatCore.shared.stories().filter { wanted.contains($0.id) }.map(StoryEntity.init)
    }

    @MainActor
    func suggestedEntities() async throws -> [StoryEntity] {
        await MaatCore.shared.stories().prefix(10).map(StoryEntity.init)
    }
}

extension StoryEntityQuery: EntityStringQuery {
    @MainActor
    func entities(matching string: String) async throws -> [StoryEntity] {
        let stories = await MaatCore.shared.stories()
        return SemanticSearch().search(string, in: stories).map(StoryEntity.init)
    }
}
