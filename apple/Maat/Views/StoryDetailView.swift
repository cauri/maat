import SwiftUI

#if canImport(Translation)
import Translation
#endif

struct StoryDetailView: View {
    let story: Story

    @Environment(AppSettings.self) private var settings
    @Environment(FeedStore.self) private var feed
    @Environment(Analytics.self) private var analytics

    @State private var summary: String?
    @State private var summarizing = false

    @State private var translator = TranslationController()
    @State private var showTranslated = false
    @State private var translating = false
    @State private var translatedFact: String?
    @State private var translatedClaims: [String: String] = [:]

    @State private var deeper: Deeper?
    @State private var loadingDeeper = false

    @State private var feedback: EngagementSignal?
    @State private var openedAt = Date.now

    private var needsTranslation: Bool { story.primaryLanguage != settings.displayLanguageCode }
    private var displayFact: String { (showTranslated ? translatedFact : nil) ?? story.fact }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                summarySection
                actionsSection
                originatorsSection
                claimsSection
                deeperSection
                commentsLink
            }
            .padding()
        }
        .background(Palette.bg)
        .navigationTitle("Story")
        #if canImport(Translation)
        .translationTask(translator.configuration) { session in
            await translator.fulfill(using: session)
        }
        #endif
        .task { await summarize() }
        .onAppear {
            openedAt = .now
            analytics.record(.storyOpened, storyID: story.id)
        }
        .onDisappear {
            let dwell = Date.now.timeIntervalSince(openedAt)
            analytics.record(dwell > 6 ? .readWhole : .abandonedHalf, storyID: story.id)
        }
    }

    // MARK: Sections

    private var header: some View {
        card {
            HStack {
                ExtremityChip(extremity: story.extremity)
                Spacer(minLength: 8)
                if story.hasPrimary { Chip(text: "primary source", style: .fact) }
            }
            Text(displayFact)
                .font(.title3.weight(.semibold))
                .foregroundStyle(Palette.ink)
                .fixedSize(horizontal: false, vertical: true)
            ConfidenceBar(story: story)
        }
    }

    private var summarySection: some View {
        card(title: "Summary") {
            if summarizing {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("Summarising on-device…").foregroundStyle(Palette.muted)
                }
            } else {
                Text((showTranslated ? translatedFact : nil) ?? summary ?? "—")
                    .foregroundStyle(Palette.ink)
            }
            if !Intelligence.isAvailable {
                Text(Intelligence.statusDescription)
                    .font(.caption2)
                    .foregroundStyle(Palette.muted)
            }
        }
    }

    private var actionsSection: some View {
        card(title: "This story") {
            if needsTranslation {
                Button {
                    Task { await toggleTranslate() }
                } label: {
                    HStack(spacing: 6) {
                        if translating { ProgressView() }
                        Image(systemName: "character.bubble")
                        Text(showTranslated
                             ? "Show original (\(story.primaryLanguage.uppercased()))"
                             : "Translate to \(settings.displayLanguageCode.uppercased())")
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(translating)
            }
            HStack(spacing: 12) {
                feedbackButton(.feedbackUp, icon: "hand.thumbsup")
                feedbackButton(.feedbackDown, icon: "hand.thumbsdown")
                Spacer(minLength: 0)
            }
        }
    }

    private var originatorsSection: some View {
        card(title: "Corroboration · independent originators") {
            OriginatorList(groups: story.originatorGroups)
        }
    }

    private var claimsSection: some View {
        card(title: "Claims") {
            ForEach(story.claims) { claim in
                VStack(alignment: .leading, spacing: 6) {
                    ClaimBadges(claim: claim)
                    Text((showTranslated ? translatedClaims[claim.id] : nil) ?? claim.text)
                        .foregroundStyle(Palette.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    if let source = claim.source {
                        Text(source).font(.caption2).foregroundStyle(Palette.muted)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 8)
                if claim.id != story.claims.last?.id {
                    Divider().overlay(Palette.line)
                }
            }
        }
    }

    private var deeperSection: some View {
        card(title: "Go deeper") {
            if let deeper {
                Text(deeper.note).font(.footnote).foregroundStyle(Palette.muted)
                ForEach(deeper.provenance) { p in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(p.source ?? "—").font(.subheadline.weight(.semibold))
                        HStack(spacing: 6) {
                            Chip(text: p.voice.label, style: p.voice.chip)
                            if let span = p.evidenceSpan {
                                Text("“\(span)”").font(.caption).italic().foregroundStyle(Palette.muted)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 4)
                }
            } else {
                Button {
                    Task { await goDeeper() }
                } label: {
                    HStack(spacing: 6) {
                        if loadingDeeper { ProgressView() }
                        Image(systemName: "arrow.down.right.and.arrow.up.left")
                        Text("Go deeper on this story")
                    }
                }
                .buttonStyle(.bordered)
                .disabled(loadingDeeper)
                Text("Escalates to the server / Private Cloud Compute tier for full provenance.")
                    .font(.caption2).foregroundStyle(Palette.muted)
            }
        }
    }

    private var commentsLink: some View {
        NavigationLink {
            CommentsView(story: story)
        } label: {
            card(title: "Comments") {
                HStack {
                    Text("Your notes on this story").foregroundStyle(Palette.ink)
                    Spacer()
                    Image(systemName: "chevron.right").foregroundStyle(Palette.muted)
                }
                Text("Stored locally on this device.").font(.caption2).foregroundStyle(Palette.muted)
            }
        }
        .buttonStyle(.plain)
    }

    private func feedbackButton(_ signal: EngagementSignal, icon: String) -> some View {
        Button {
            feedback = signal
            analytics.record(signal, storyID: story.id)
        } label: {
            Image(systemName: feedback == signal ? "\(icon).fill" : icon)
        }
        .buttonStyle(.bordered)
        .tint(feedback == signal ? Palette.confHigh : Palette.muted)
    }

    // MARK: Card chrome

    @ViewBuilder
    private func card<Content: View>(title: String? = nil, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if let title {
                Text(title)
                    .font(.caption.weight(.semibold))
                    .textCase(.uppercase)
                    .foregroundStyle(Palette.muted)
            }
            content()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.line, lineWidth: 1))
    }

    // MARK: Actions

    private func summarize() async {
        guard summary == nil else { return }
        summarizing = true
        defer { summarizing = false }
        summary = await FoundationModelsSummarizer().summarize(story)
        analytics.record(.readSummary, storyID: story.id)
    }

    private func toggleTranslate() async {
        if showTranslated {
            showTranslated = false
            return
        }
        if translatedFact == nil {
            translating = true
            defer { translating = false }
            translator.preferOnDevice = settings.preferOnDeviceTranslation
            translator.cloud = settings.apiBaseURL.isEmpty
                ? nil
                : URL(string: settings.apiBaseURL).map { CloudTranslator(baseURL: $0) }
            let texts = [story.fact] + story.claims.map(\.text)
            let out = await translator.translate(
                texts, to: settings.displayLanguageCode, from: story.primaryLanguage
            )
            if out.count == texts.count {
                translatedFact = out[0]
                for (index, claim) in story.claims.enumerated() {
                    translatedClaims[claim.id] = out[index + 1]
                }
            }
            analytics.record(.translated, storyID: story.id)
        }
        showTranslated = translatedFact != nil
    }

    private func goDeeper() async {
        loadingDeeper = true
        defer { loadingDeeper = false }
        if let expanded = try? await feed.loadStory(id: story.id, deeper: true) {
            deeper = expanded.deeper
        }
        analytics.record(.wentDeeper, storyID: story.id)
    }
}
