import SwiftUI

/// A titled white card — the shared container for detail/source sections.
struct SectionCard<Content: View>: View {
    var title: String?
    @ViewBuilder var content: Content

    init(title: String? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let title {
                Text(title)
                    .font(.caption.weight(.semibold))
                    .textCase(.uppercase)
                    .foregroundStyle(Palette.muted)
            }
            content
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.line, lineWidth: 1))
    }
}

/// A pill, matching the web reader's `.b` chips.
struct Chip: View {
    var text: String
    var style: ChipStyle

    var body: some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 8)
            .padding(.vertical, 2)
            .background(style.bg, in: Capsule())
            .foregroundStyle(style.fg)
    }
}

/// The veracity chips for one claim: headline · voice · fact/projection · synthesis (§5.2–5.3).
struct ClaimBadges: View {
    var claim: Claim

    var body: some View {
        FlowRow(spacing: 5) {
            if claim.inHeadline {
                Chip(text: "headline", style: .headline)
            }
            if claim.voice == .attributed {
                Chip(text: "said · \(claim.speaker ?? "?")", style: .attributed)
            } else {
                Chip(text: "own voice", style: .own)
            }
            switch claim.kind {
            case .fact:
                Chip(text: "fact", style: .fact)
            case .projection:
                Chip(text: "projection" + (claim.horizon.map { " · \($0)" } ?? ""), style: .projection)
            case nil:
                EmptyView()
            }
            if claim.isSynthesis {
                Chip(text: "synthesis", style: .synthesis)
            }
        }
    }
}

struct ExtremityChip: View {
    var extremity: Extremity
    var body: some View {
        Chip(text: extremity.label, style: extremity.chip)
    }
}

/// A minimal wrapping HStack so chips flow onto multiple lines (Layout, iOS 16+).
struct FlowRow: Layout {
    var spacing: CGFloat = 5

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout Void) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var rowWidth: CGFloat = 0, rowHeight: CGFloat = 0
        var totalHeight: CGFloat = 0, totalWidth: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if rowWidth + size.width > maxWidth, rowWidth > 0 {
                totalWidth = max(totalWidth, rowWidth - spacing)
                totalHeight += rowHeight + spacing
                rowWidth = 0
                rowHeight = 0
            }
            rowWidth += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        totalWidth = max(totalWidth, rowWidth - spacing)
        totalHeight += rowHeight
        return CGSize(width: min(totalWidth, maxWidth), height: totalHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout Void) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x + size.width > bounds.maxX, x > bounds.minX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}
