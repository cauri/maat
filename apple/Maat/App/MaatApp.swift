import SwiftUI
import SwiftData

@main
struct MaatApp: App {
    @State private var settings = AppSettings()
    @State private var topics = TopicStore()
    @State private var analytics = Analytics()
    @State private var feed = FeedStore(primary: FixtureFeedService())

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(settings)
                .environment(topics)
                .environment(analytics)
                .environment(feed)
        }
        .modelContainer(for: Comment.self)
    }
}
