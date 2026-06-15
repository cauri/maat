import SwiftUI

struct SearchView: View {
    @Environment(FeedStore.self) private var feed
    @Environment(Analytics.self) private var analytics

    @State private var query = ""
    @State private var results: [Story] = []

    var body: some View {
        NavigationStack {
            Group {
                if query.trimmingCharacters(in: .whitespaces).isEmpty {
                    ContentUnavailableView(
                        "Search the feed",
                        systemImage: "magnifyingglass",
                        description: Text("Semantic search runs on-device. Your query never leaves the phone.")
                    )
                } else if results.isEmpty {
                    ContentUnavailableView.search(text: query)
                } else {
                    List(results) { story in
                        NavigationLink(value: story) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(story.fact).font(.subheadline.weight(.medium))
                                Text("\(story.confidenceWord) · \(story.corroborationSummary)")
                                    .font(.caption2).foregroundStyle(Palette.muted)
                            }
                        }
                    }
                }
            }
            .navigationTitle("Search")
            .navigationDestination(for: Story.self) { StoryDetailView(story: $0) }
            .searchable(text: $query, prompt: "Search stories")
            .onChange(of: query) { _, newValue in
                results = SemanticSearch().search(newValue, in: feed.stories)
            }
            .onSubmit(of: .search) {
                if !query.trimmingCharacters(in: .whitespaces).isEmpty {
                    analytics.record(.searched)
                }
            }
        }
    }
}
