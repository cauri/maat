import SwiftUI

/// The independent-originator collapse (§5.5): each row is one originator; a collapsed row is wire
/// syndication / a citation cascade folded to a single node. Mirrors the web reader's `.orig`.
struct OriginatorList: View {
    var groups: [OriginatorGroup]

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(Array(groups.enumerated()), id: \.offset) { _, group in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(group.collapsed ? "WIRE · COLLAPSED" : "INDEPENDENT")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(Palette.muted)
                    Text(group.sources.joined(separator: ", "))
                        .font(.footnote)
                        .foregroundStyle(Palette.ink)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 11)
                .padding(.vertical, 6)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(group.collapsed ? Palette.wireBg : Palette.indepBg,
                            in: RoundedRectangle(cornerRadius: 9))
            }
        }
    }
}
