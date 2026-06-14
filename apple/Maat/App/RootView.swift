import SwiftUI

struct RootView: View {
    @Environment(AppRouter.self) private var router

    var body: some View {
        @Bindable var router = router
        TabView(selection: $router.selectedTab) {
            Tab("Feed", systemImage: "newspaper", value: AppTab.feed) {
                FeedView()
            }
            Tab("Search", systemImage: "magnifyingglass", value: AppTab.search) {
                SearchView()
            }
            Tab("Topics", systemImage: "tag", value: AppTab.topics) {
                TopicsView()
            }
            Tab("Settings", systemImage: "gearshape", value: AppTab.settings) {
                SettingsView()
            }
        }
        .task {
            await MaatCore.shared.bootstrap()
        }
    }
}
