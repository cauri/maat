import SwiftUI

struct FeedView: View {
    @Environment(FeedStore.self) private var feed
    @Environment(TopicStore.self) private var topics
    @Environment(AppRouter.self) private var router
    @State private var showSettings = false

    var body: some View {
        @Bindable var router = router
        NavigationStack(path: $router.feedPath) {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if feed.usingFallback, let error = feed.error {
                        FallbackBanner(message: error).padding(.bottom, 14)
                    }

                    let stories = feed.displayStories
                    if let lead = stories.first {
                        NavigationLink(value: lead) { LeadStoryCard(story: lead) }
                            .buttonStyle(.plain)

                        if stories.count > 1 {
                            Text("More stories")
                                .font(.caption.weight(.semibold))
                                .textCase(.uppercase)
                                .foregroundStyle(Palette.muted)
                                .padding(.top, 20)
                        }
                        ForEach(stories.dropFirst()) { story in
                            NavigationLink(value: story) { StoryRow(story: story) }
                                .buttonStyle(.plain)
                            Divider().overlay(Palette.line)
                        }
                    } else if !feed.isLoading {
                        ContentUnavailableView(
                            "No stories yet",
                            systemImage: "newspaper",
                            description: Text("Start the agents and ingest a corpus, or point Settings at a reader.")
                        )
                        .padding(.top, 60)
                    }
                }
                .padding()
            }
            .background(Palette.bg)
            .navigationTitle("Today")
            .navigationDestination(for: Story.self) { StoryDetailView(story: $0) }
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { showSettings = true } label: {
                        Image(systemName: "gearshape")
                    }
                    .accessibilityLabel("Settings")
                }
            }
            .sheet(isPresented: $showSettings) { SettingsView() }
            .overlay {
                if feed.isLoading, feed.stories.isEmpty { ProgressView() }
            }
            .refreshable {
                await feed.refresh()
                await feed.applyRerank(FoundationModelsReranker(), topics: topics.topics)
                await feed.refreshSources()
            }
        }
    }
}

struct FallbackBanner: View {
    var message: String
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "internaldrive")
            Text(message).font(.footnote)
            Spacer(minLength: 0)
        }
        .foregroundStyle(Palette.confMid)
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.wireBg, in: RoundedRectangle(cornerRadius: 10))
    }
}
