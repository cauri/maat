import SwiftUI

/// A story card in the feed: extremity + primary-source badges, the fact, and the confidence read.
struct StoryRow: View {
    var story: Story

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                ExtremityChip(extremity: story.extremity)
                Spacer(minLength: 8)
                if story.hasPrimary {
                    Chip(text: "primary source", style: .fact)
                }
            }

            Text(story.fact)
                .font(.headline)
                .foregroundStyle(Palette.ink)
                .fixedSize(horizontal: false, vertical: true)

            ConfidenceBar(story: story)

            if story.primaryLanguage != "en" {
                Chip(text: "original: \(story.primaryLanguage.uppercased())", style: .neutral)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.line, lineWidth: 1))
    }
}
