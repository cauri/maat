import SwiftUI

struct FeedView: View {
    @Environment(FeedStore.self) private var feed
    @Environment(TopicStore.self) private var topics

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(spacing: 14) {
                    if feed.usingFallback, let error = feed.error {
                        FallbackBanner(message: error)
                    }
                    ForEach(feed.displayStories) { story in
                        NavigationLink(value: story) {
                            StoryRow(story: story)
                        }
                        .buttonStyle(.plain)
                    }
                    if feed.displayStories.isEmpty, !feed.isLoading {
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
            .navigationTitle("Maat")
            .navigationDestination(for: Story.self) { StoryDetailView(story: $0) }
            .overlay {
                if feed.isLoading, feed.stories.isEmpty {
                    ProgressView()
                }
            }
            .refreshable {
                await feed.refresh()
                await feed.applyRerank(FoundationModelsReranker(), topics: topics.topics)
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
