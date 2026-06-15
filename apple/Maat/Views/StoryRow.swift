import SwiftUI

// Editorial story cells — Apple News feel (BRIEF §1). The corroborated fact is the headline; the
// sources line and a quiet confidence cue sit beneath. Veracity detail lives in StoryDetailView.

private func sourceLine(_ story: Story, withCount: Bool = true) -> String {
    // Lead with independent originators, wire reprints last (corroboration over spread, §5.5).
    let names = story.originatorGroups
        .sorted { !$0.collapsed && $1.collapsed }
        .flatMap(\.sources)
    let shown = names.prefix(2).joined(separator: " · ")
    let extra = names.count > 2 ? " · +\(names.count - 2)" : ""
    let count = withCount ? " — \(story.independentOriginators) independent" : ""
    return shown + extra + count
}

private extension Story.ConfidenceLevel {
    var chip: ChipStyle {
        switch self {
        case .high: return .fact
        case .medium: return .projection
        case .low: return .extraordinary
        }
    }
}

/// The featured lead story at the top of Today.
struct LeadStoryCard: View {
    var story: Story
    @Environment(AppSettings.self) private var settings

    private var heroURL: URL? {
        story.heroImageArticleId.flatMap { settings.imageURL(articleID: $0) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let heroURL {
                StoryThumbnail(url: heroURL, height: 188, corner: 12)
            }
            HStack(spacing: 6) {
                Chip(text: story.confidenceWord, style: story.confidenceLevel.chip)
                if story.hasPrimary { Chip(text: "primary-source backed", style: .attributed) }
                Spacer(minLength: 0)
            }
            Text(story.fact)
                .font(.system(.title2, design: .serif).weight(.semibold))
                .foregroundStyle(Palette.ink)
                .fixedSize(horizontal: false, vertical: true)
            Text(sourceLine(story))
                .font(.subheadline)
                .foregroundStyle(Palette.muted)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(Palette.line, lineWidth: 1))
    }
}

/// A standard story cell in the Today list.
struct StoryRow: View {
    var story: Story
    @Environment(AppSettings.self) private var settings

    private var thumbURL: URL? {
        story.heroImageArticleId.flatMap { settings.imageURL(articleID: $0) }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                Text(story.fact)
                    .font(.system(.headline, design: .serif))
                    .foregroundStyle(Palette.ink)
                    .fixedSize(horizontal: false, vertical: true)
                Text(sourceLine(story))
                    .font(.caption)
                    .foregroundStyle(Palette.muted)
                    .lineLimit(2)
                HStack(spacing: 6) {
                    Text(story.confidenceWord)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(story.confidenceLevel.color)
                    if story.extremity == .extraordinary {
                        Chip(text: "extraordinary claim", style: .extraordinary)
                    }
                    if story.primaryLanguage != "en" {
                        Chip(text: "original: \(story.primaryLanguage.uppercased())", style: .neutral)
                    }
                    Spacer(minLength: 0)
                }
            }
            if let thumbURL {
                StoryThumbnail(url: thumbURL, width: 84, height: 84, corner: 8)
            }
        }
        .padding(.vertical, 11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
    }
}
