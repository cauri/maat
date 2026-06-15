import AppIntents
import CoreSpotlight
import Foundation

// A story exposed to Siri / Shortcuts / Spotlight (#80, #83). Backed by the same feed the UI shows
// (via `IntentDataSource`, which works in or out of the app process); the string query reuses
// on-device semantic search so "search Maat for …" works without the query ever leaving the phone.
//
// Conforms to `IndexedEntity` (#83): the framework can hand each entity to Spotlight as a
// `CSSearchableItem`, and a Spotlight hit on a donated story resolves back to this entity (and thus
// the "Show a Maat Story" intent) by matching the searchable item's identifier to the entity id.

struct StoryEntity: IndexedEntity, Identifiable {
    static let typeDisplayRepresentation = TypeDisplayRepresentation(name: "Story")
    static let defaultQuery = StoryEntityQuery()

    let id: String
    let fact: String
    let confidenceWord: String
    let independentOriginators: Int
    let hasPrimary: Bool
    let corroborationSummary: String
    /// Claim texts, kept so Spotlight can index the story's substance, not just its one-line fact.
    let claimTexts: [String]

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(
            title: "\(fact)",
            subtitle: "\(confidenceWord) · \(independentOriginators) independent originators"
        )
    }

    /// The Spotlight record for this entity (`IndexedEntity`). Mirrors `SpotlightDonor.attributeSet`
    /// so donations the framework drives and donations we drive describe a story identically.
    var attributeSet: CSSearchableItemAttributeSet {
        let attributes = CSSearchableItemAttributeSet(contentType: .text)
        attributes.title = fact
        attributes.contentDescription = "\(confidenceWord) · \(corroborationSummary)"
        attributes.keywords = [fact] + claimTexts
        return attributes
    }

    init(_ story: Story) {
        id = story.id
        fact = story.fact
        confidenceWord = story.confidenceWord
        independentOriginators = story.independentOriginators
        hasPrimary = story.hasPrimary
        corroborationSummary = story.corroborationSummary
        claimTexts = story.claims.map(\.text)
    }
}

struct StoryEntityQuery: EntityQuery {
    func entities(for identifiers: [String]) async throws -> [StoryEntity] {
        let wanted = Set(identifiers)
        return await IntentDataSource.shared.stories().filter { wanted.contains($0.id) }.map(StoryEntity.init)
    }

    func suggestedEntities() async throws -> [StoryEntity] {
        await IntentDataSource.shared.stories().prefix(10).map(StoryEntity.init)
    }
}

extension StoryEntityQuery: EntityStringQuery {
    func entities(matching string: String) async throws -> [StoryEntity] {
        let stories = await IntentDataSource.shared.stories()
        return SemanticSearch().search(string, in: stories).map(StoryEntity.init)
    }
}
