import SwiftUI

struct RootView: View {
    @Environment(AppSettings.self) private var settings
    @Environment(TopicStore.self) private var topics
    @Environment(FeedStore.self) private var feed

    var body: some View {
        TabView {
            Tab("Feed", systemImage: "newspaper") {
                FeedView()
            }
            Tab("Search", systemImage: "magnifyingglass") {
                SearchView()
            }
            Tab("Topics", systemImage: "tag") {
                TopicsView()
            }
            Tab("Settings", systemImage: "gearshape") {
                SettingsView()
            }
        }
        .task {
            feed.setService(settings.makeFeedService())
            await feed.refresh()
            await feed.applyRerank(FoundationModelsReranker(), topics: topics.topics)
        }
    }
}
