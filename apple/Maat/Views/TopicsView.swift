import SwiftUI

struct TopicsView: View {
    @Environment(TopicStore.self) private var topics
    @Environment(FeedStore.self) private var feed

    @State private var newTopic = ""

    var body: some View {
        NavigationStack {
            List {
                Section("Your topics") {
                    ForEach(topics.topics, id: \.self) { topic in
                        Text(topic)
                    }
                    .onDelete { offsets in
                        topics.remove(at: offsets)
                        rerank()
                    }
                    HStack {
                        TextField("Add a topic…", text: $newTopic)
                            .onSubmit(add)
                        Button("Add", action: add)
                            .disabled(newTopic.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
                Section {
                    Text("Topics re-rank your feed on-device against what you care about. They never leave your phone.")
                        .font(.footnote)
                        .foregroundStyle(Palette.muted)
                }
            }
            .navigationTitle("Topics")
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
