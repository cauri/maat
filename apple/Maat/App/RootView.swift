import SwiftUI
import CoreSpotlight

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
        // A Spotlight tap on a donated story (#83) launches the app here — resolve the item id back to
        // the story and open it. The donated identifier is namespaced by `SpotlightDonor`.
        .onContinueUserActivity(CSSearchableItemActionType) { activity in
            guard let identifier = activity.userInfo?[CSSearchableItemActivityIdentifier] as? String,
                  let storyID = SpotlightDonor.storyID(fromEntityIdentifier: identifier)
            else { return }
            Task {
                if let story = await MaatCore.shared.story(id: storyID) {
                    MaatCore.shared.router.open(story)
                    MaatCore.shared.analytics.record(.storyOpened, storyID: storyID)
                }
            }
        }
    }
}
