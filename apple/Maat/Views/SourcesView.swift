import SwiftUI

// The reputation surface (BRIEF §6): news organisations ranked by truthfulness, each with a trajectory
// sparkline (§6.4) and a plain-language tier. Cold-start orgs are shown neutrally (§6.6).

struct SourcesView: View {
    @Environment(FeedStore.self) private var feed

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(feed.sources) { rating in
                        NavigationLink(value: rating) { SourceRow(rating: rating) }
                    }
                    if feed.sources.isEmpty {
                        Text("No sources yet.").foregroundStyle(Palette.muted)
                    }
                } header: {
                    Text("Truthfulness over time")
                } footer: {
                    Text("Provisional — reputation is approximated from corroboration until truth-over-time scoring is built. Cold-start sources are shown neutrally, never as untrustworthy.")
                }
            }
            .navigationTitle("Sources")
            .navigationDestination(for: SourceRating.self) { SourceDetailView(rating: $0) }
            .refreshable { await feed.refreshSources() }
        }
    }
}

struct SourceRow: View {
    var rating: SourceRating

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(rating.name)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Palette.ink)
                    .lineLimit(2)
                Text(rating.tier)
                    .font(.caption)
                    .foregroundStyle(rating.band.color)
            }
            Spacer(minLength: 8)
            Sparkline(values: rating.trajectory, color: rating.band.color)
                .frame(width: 56, height: 18)
        }
        .padding(.vertical, 4)
    }
}

struct SourceDetailView: View {
    var rating: SourceRating
    @Environment(FeedStore.self) private var feed
    @State private var detail: SourceRating?

    private var r: SourceRating { detail ?? rating }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                SectionCard {
                    Text(r.displayTier)
                        .font(.title.weight(.semibold))
                        .foregroundStyle(r.coldStart ? Palette.muted : r.band.color)
                    Text(r.isPrimary
                         ? "Primary source · highest standing"
                         : "How truthfully this source has reported over time")
                        .font(.caption).foregroundStyle(Palette.muted)
                    if !r.languages.isEmpty {
                        Text(r.languages.map { $0.uppercased() }.joined(separator: " · "))
                            .font(.caption2).foregroundStyle(Palette.muted)
                    }
                    Sparkline(values: r.trajectory, color: r.band.color).frame(height: 40)
                    Text("Shown as a trend over time, not a score — provisional until truth-over-time scoring lands.")
                        .font(.caption2).foregroundStyle(Palette.muted)
                }

                SectionCard(title: "Stories from this source") {
                    let stories = r.stories ?? []
                    if stories.isEmpty {
                        Text("None in the current feed.").foregroundStyle(Palette.muted)
                    }
                    ForEach(stories) { ref in
                        NavigationLink {
                            StoryLoaderView(id: ref.id)
                        } label: {
                            HStack(alignment: .firstTextBaseline) {
                                Text(ref.fact).font(.subheadline).foregroundStyle(Palette.ink)
                                Spacer(minLength: 8)
                                Text(Story.ConfidenceLevel.of(ref.confidence).word)
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(Story.ConfidenceLevel.of(ref.confidence).color)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        if ref.id != stories.last?.id { Divider().overlay(Palette.line) }
                    }
                }
            }
            .padding()
        }
        .background(Palette.bg)
        .navigationTitle(r.name)
        .task { detail = try? await feed.loadSource(name: rating.name) }
    }
}

/// Loads a full story by id (e.g. tapped from a source's story list) then shows its detail.
struct StoryLoaderView: View {
    var id: String
    @Environment(FeedStore.self) private var feed
    @State private var story: Story?

    var body: some View {
        Group {
            if let story {
                StoryDetailView(story: story)
            } else {
                ProgressView()
                    .task { story = try? await feed.loadStory(id: id, deeper: false) }
            }
        }
    }
}
