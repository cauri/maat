import AppIntents

// Spoken phrases Siri/Spotlight surface automatically — no per-user setup (#80). `\(.applicationName)`
// expands to "Maat"; the parameterised phrases let Siri capture the query/topic inline.
struct MaatShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: OpenFeedIntent(),
            phrases: [
                "Open my \(.applicationName) feed",
                "Show my \(.applicationName) feed"
            ],
            shortTitle: "Open Feed",
            systemImageName: "newspaper"
        )
        AppShortcut(
            intent: TopStoryIntent(),
            phrases: [
                "What's the top story on \(.applicationName)",
                "\(.applicationName) top story"
            ],
            shortTitle: "Top Story",
            systemImageName: "sparkles"
        )
        // Free-text params (String) can't appear in spoken phrases — only AppEntity/AppEnum can — so
        // Siri prompts for the query/topic via each intent's requestValueDialog.
        AppShortcut(
            intent: SearchStoriesIntent(),
            phrases: [
                "Search \(.applicationName)",
                "Search for stories on \(.applicationName)"
            ],
            shortTitle: "Search Stories",
            systemImageName: "magnifyingglass"
        )
        AppShortcut(
            intent: AddTopicIntent(),
            phrases: [
                "Add a topic to \(.applicationName)",
                "Add a \(.applicationName) topic"
            ],
            shortTitle: "Add Topic",
            systemImageName: "tag"
        )
    }
}
