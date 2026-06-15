import SwiftUI

// Article lead images (#1), loaded through the reader's privacy-preserving proxy (the client passes
// an article id, never the origin URL). Images are enrichment, never a veracity signal — so loading
// and failure states stay quiet and never block the read. Only render this when a URL exists.

struct StoryThumbnail: View {
    let url: URL?
    /// Fixed width for a list-row thumbnail; nil fills the available width (a hero image).
    var width: CGFloat? = nil
    var height: CGFloat
    var corner: CGFloat = 10

    var body: some View {
        AsyncImage(url: url, transaction: Transaction(animation: .easeInOut(duration: 0.2))) { phase in
            switch phase {
            case .success(let image):
                image.resizable().scaledToFill()
            case .failure:
                placeholder(icon: "photo")
            case .empty:
                placeholder(icon: nil)  // loading
            @unknown default:
                placeholder(icon: nil)
            }
        }
        .frame(width: width, height: height)
        .frame(maxWidth: width == nil ? .infinity : nil)
        .clipped()
        .clipShape(RoundedRectangle(cornerRadius: corner))
        .accessibilityHidden(true)  // decorative; the headline carries the meaning
    }

    private func placeholder(icon: String?) -> some View {
        ZStack {
            Palette.line
            if let icon {
                Image(systemName: icon).foregroundStyle(Palette.muted)
            } else {
                ProgressView().tint(Palette.muted)
            }
        }
    }
}
