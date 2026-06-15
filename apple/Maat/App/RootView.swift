import SwiftUI

struct RootView: View {
    @Environment(AppRouter.self) private var router

    var body: some View {
        @Bindable var router = router
        TabView(selection: $router.selectedTab) {
            Tab("Today", systemImage: "newspaper", value: AppTab.feed) {
                FeedView()
            }
            Tab("Sources", systemImage: "building.columns", value: AppTab.sources) {
                SourcesView()
            }
            Tab("Search", systemImage: "magnifyingglass", value: AppTab.search) {
                SearchView()
            }
            Tab("Following", systemImage: "bookmark", value: AppTab.following) {
                FollowingView()
            }
        }
        .task {
            await MaatCore.shared.bootstrap()
        }
    }
}
