import SwiftUI

/// The confidence read (§5.6–5.7): a traffic-light bar scaled by the story's confidence, the percent,
/// and the corroboration summary (sources → independent originators). Mirrors the web reader's `.conf`.
struct ConfidenceBar: View {
    var story: Story

    private var level: Story.ConfidenceLevel { story.confidenceLevel }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 9) {
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Palette.line)
                        Capsule()
                            .fill(level.color)
                            .frame(width: max(0, geo.size.width * story.confidence))
                    }
                }
                .frame(height: 7)

                Text(story.confidenceWord)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(level.color)
                    .fixedSize()
            }
            .frame(height: 18)

            Text(story.corroborationSummary)
                .font(.footnote)
                .foregroundStyle(Palette.ink.opacity(0.8))
        }
    }
}
