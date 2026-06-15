import SwiftUI
#if os(iOS)
import SafariServices
#endif

// Link back to the original article (#2) — courtesy to the publisher whose reporting this is. We only
// ever redirect to the publisher's own page; there's no money in it, just attribution (cauri). On iOS
// it opens an in-app Safari view so the reader is one tap from coming back; on macOS it opens the
// default browser.

struct ReadOriginalButton: View {
    let url: URL
    var source: String?

    @Environment(\.openURL) private var openURL
    @State private var showSafari = false

    private var label: String {
        if let source, !source.isEmpty { return "Read at \(source)" }
        return "Read the original"
    }

    var body: some View {
        Button {
            #if os(iOS)
            showSafari = true
            #else
            openURL(url)
            #endif
        } label: {
            HStack(spacing: 10) {
                Image(systemName: "safari")
                VStack(alignment: .leading, spacing: 1) {
                    Text(label).font(.subheadline.weight(.medium)).foregroundStyle(Palette.ink)
                    if let host = url.host() {
                        Text(host).font(.caption2).foregroundStyle(Palette.muted)
                    }
                }
                Spacer(minLength: 0)
                Image(systemName: "arrow.up.right").font(.caption).foregroundStyle(Palette.muted)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .tint(Palette.ink)
        #if os(iOS)
        .sheet(isPresented: $showSafari) {
            SafariView(url: url).ignoresSafeArea()
        }
        #endif
    }
}

#if os(iOS)
/// In-app Safari (SFSafariViewController) so the original opens without leaving Maat.
private struct SafariView: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> SFSafariViewController {
        SFSafariViewController(url: url)
    }
    func updateUIViewController(_ controller: SFSafariViewController, context: Context) {}
}
#endif
