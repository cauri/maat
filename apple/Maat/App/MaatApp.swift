import SwiftUI
import SwiftData

@main
struct MaatApp: App {
    private let core = MaatCore.shared

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(core.settings)
                .environment(core.topics)
                .environment(core.analytics)
                .environment(core.feed)
                .environment(core.router)
        }
        .modelContainer(for: Comment.self)
    }
}
