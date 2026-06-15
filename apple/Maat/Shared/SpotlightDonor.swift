import Foundation
import CoreSpotlight
import UniformTypeIdentifiers

// Donate stories to Spotlight so they're searchable from the system, not just inside the app (#83).
// Each story becomes a `CSSearchableItem`; because `StoryEntity` is an `IndexedEntity`, the App
// Intents framework links the indexed item back to the entity, so a Spotlight hit can drive the
// "Show a Maat Story" intent. Everything indexed here is already on-device public feed text — no
// per-user signal is donated (PLAN §6).
enum SpotlightDonor {
    /// All Maat stories share one Spotlight domain so a refresh can replace the set wholesale.
    static let domain = "dev.cauri.maat.stories"

    /// Reindex the donated stories: clear the whole Maat-stories domain, then index the current feed —
    /// so a story that dropped out of the feed doesn't leave a stale Spotlight result behind. Indexing
    /// runs only after the clear completes. Best-effort — Spotlight failures never surface to the reader.
    static func donate(_ stories: [Story]) {
        let index = CSSearchableIndex.default()
        let items = stories.map(searchableItem)
        index.deleteSearchableItems(withDomainIdentifiers: [domain]) { _ in
            index.indexSearchableItems(items) { _ in }
        }
    }

    /// Build the Spotlight record for one story. The unique identifier matches `StoryEntity`'s, so the
    /// `IndexedEntity` association resolves a Spotlight tap straight back to the entity.
    static func searchableItem(for story: Story) -> CSSearchableItem {
        CSSearchableItem(
            uniqueIdentifier: entityIdentifier(for: story.id),
            domainIdentifier: domain,
            attributeSet: attributeSet(for: story)
        )
    }

    static func attributeSet(for story: Story) -> CSSearchableItemAttributeSet {
        let attributes = CSSearchableItemAttributeSet(contentType: .text)
        attributes.title = story.fact
        attributes.contentDescription = "\(story.confidenceWord) · \(story.corroborationSummary)"
        // Searchable keywords: the headline plus each claim's text, so a Spotlight query matches the
        // story's substance, not only its one-line fact.
        attributes.keywords = [story.fact] + story.claims.map(\.text)
        return attributes
    }

    /// Spotlight item id ⇄ `StoryEntity.ID`. Namespaced so it can't collide with other indexed types.
    static func entityIdentifier(for storyID: String) -> String { "\(domain).\(storyID)" }

    static func storyID(fromEntityIdentifier identifier: String) -> String? {
        guard identifier.hasPrefix("\(domain).") else { return nil }
        return String(identifier.dropFirst(domain.count + 1))
    }
}
