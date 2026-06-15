import SwiftUI

// Following = the reader's pinned stories (BRIEF §1) + natural-language topics (§4). Both per-user,
// on-device; topics steer the feed re-rank.

struct FollowingView: View {
    @Environment(FeedStore.self) private var feed
    @Environment(TopicStore.self) private var topics
    @Environment(PinStore.self) private var pins
    @State private var newTopic = ""

    private var pinnedStories: [Story] {
        feed.stories.filter { pins.isPinned($0.id) }
    }

    var body: some View {
        NavigationStack {
            List {
                Section("Pinned stories") {
                    if pinnedStories.isEmpty {
                        Text("Pin a story to follow it.").foregroundStyle(Palette.muted)
                    }
                    ForEach(pinnedStories) { story in
                        NavigationLink(value: story) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(story.fact).font(.subheadline).foregroundStyle(Palette.ink).lineLimit(2)
                                Text(story.confidenceWord).font(.caption2).foregroundStyle(story.confidenceLevel.color)
                            }
                        }
                    }
                }

                Section("Topics") {
                    ForEach(topics.topics, id: \.self) { topic in
                        Text(topic)
                    }
                    .onDelete { offsets in
                        topics.remove(at: offsets)
                        rerank()
                    }
                    HStack {
                        TextField("Add a topic…", text: $newTopic).onSubmit(add)
                        Button("Add", action: add)
                            .disabled(newTopic.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }

                Section {
                    Text("Topics re-rank your feed on-device against what you care about. Pins and topics never leave your phone.")
                        .font(.footnote).foregroundStyle(Palette.muted)
                }
            }
            .navigationTitle("Following")
            .navigationDestination(for: Story.self) { StoryDetailView(story: $0) }
            #if os(iOS)
            .toolbar { EditButton() }
            #endif
        }
    }

    private func add() {
        topics.add(newTopic)
        newTopic = ""
        rerank()
    }

    private func rerank() {
        Task { await feed.applyRerank(FoundationModelsReranker(), topics: topics.topics) }
    }
}
