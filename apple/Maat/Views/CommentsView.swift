import SwiftUI
import SwiftData

struct CommentsView: View {
    let story: Story

    @Environment(\.modelContext) private var context
    @Environment(Analytics.self) private var analytics
    @Query private var comments: [Comment]
    @State private var draft = ""

    init(story: Story) {
        self.story = story
        let id = story.id
        _comments = Query(
            filter: #Predicate<Comment> { $0.storyID == id },
            sort: \.createdAt,
            order: .reverse
        )
    }

    var body: some View {
        List {
            if comments.isEmpty {
                Text("No notes yet. Anything you write stays on this device.")
                    .foregroundStyle(Palette.muted)
            }
            ForEach(comments) { comment in
                VStack(alignment: .leading, spacing: 4) {
                    Text(comment.text)
                    Text(comment.createdAt, style: .relative)
                        .font(.caption2)
                        .foregroundStyle(Palette.muted)
                }
            }
            .onDelete(perform: delete)
        }
        .navigationTitle("Comments")
        .safeAreaInset(edge: .bottom) {
            HStack(spacing: 8) {
                TextField("Add a note…", text: $draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                Button(action: add) {
                    Image(systemName: "arrow.up.circle.fill").font(.title2)
                }
                .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding()
            .background(.bar)
        }
    }

    private func add() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        context.insert(Comment(storyID: story.id, storyFact: story.fact, text: text))
        try? context.save()
        analytics.record(.commented, storyID: story.id)
        draft = ""
    }

    private func delete(_ offsets: IndexSet) {
        for index in offsets { context.delete(comments[index]) }
        try? context.save()
    }
}
