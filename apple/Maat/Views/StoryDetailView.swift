import SwiftUI

#if canImport(Translation)
import Translation
#endif

// The reader (BRIEF §1 — the primary purpose is to read the news). The full article body leads, with
// a switcher across the outlets covering the story; confidence, corroboration, reputation and the
// claim-level veracity sit beneath as *context for what you read*. Translate / pin / comment / deeper.

struct StoryDetailView: View {
    let story: Story

    @Environment(AppSettings.self) private var settings
    @Environment(FeedStore.self) private var feed
    @Environment(Analytics.self) private var analytics
    @Environment(PinStore.self) private var pins

    @State private var full: Story?
    @State private var selectedArticleID: String?

    @State private var summary: String?
    @State private var summarizing = false

    @State private var translator = TranslationController()
    @State private var showTranslated = false
    @State private var translating = false
    @State private var translatedTitle: [String: String] = [:]
    @State private var translatedBody: [String: String] = [:]
    @State private var translatedClaims: [String: String] = [:]

    @State private var deeper: Deeper?
    @State private var loadingDeeper = false
    @State private var feedback: EngagementSignal?
    @State private var openedAt = Date.now

    // MARK: Derived

    private var s: Story { full ?? story }
    private var articles: [Article] { s.articles ?? [] }
    private var hasBody: Bool { !articles.isEmpty }

    /// Most-reputable / primary article reads by default; the reader can switch outlets.
    private var defaultArticle: Article? {
        articles.max { reputationScore(of: $0) < reputationScore(of: $1) }
    }

    private var selectedArticle: Article? {
        if let id = selectedArticleID, let match = articles.first(where: { $0.id == id }) { return match }
        return defaultArticle
    }

    private func reputationScore(of article: Article) -> Double {
        guard let source = article.source, let rating = feed.rating(for: source) else { return 0.5 }
        return rating.reputation + (rating.isPrimary ? 1 : 0)
    }

    private var articleLanguage: String { selectedArticle?.language ?? s.primaryLanguage }
    private var needsTranslation: Bool { articleLanguage != settings.displayLanguageCode }

    private var displayTitle: String {
        let original = selectedArticle?.title ?? s.fact
        if showTranslated, let id = selectedArticle?.id, let t = translatedTitle[id] { return t }
        return original
    }

    private var displayBody: String {
        guard let article = selectedArticle else { return "" }
        if showTranslated, let t = translatedBody[article.id] { return t }
        return article.body
    }

    private var orderedSources: [String] {
        var seen = Set<String>()
        return s.originatorGroups
            .sorted { !$0.collapsed && $1.collapsed }
            .flatMap(\.sources)
            .filter { seen.insert($0).inserted }
    }

    private var confidenceChip: ChipStyle {
        switch s.confidenceLevel {
        case .high: return .fact
        case .medium: return .projection
        case .low: return .extraordinary
        }
    }

    // MARK: Body

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                kicker
                Text(displayTitle)
                    .font(.system(.title, design: .serif).weight(.bold))
                    .foregroundStyle(Palette.ink)
                    .fixedSize(horizontal: false, vertical: true)
                byline
                if needsTranslation { translateButton }
                Divider().overlay(Palette.line)
                articleBody
                contextSections
            }
            .padding()
        }
        .background(Palette.bg)
        .navigationTitle(selectedArticle?.source ?? "Story")
        #if canImport(Translation)
        .translationTask(translator.configuration) { session in
            await translator.fulfill(using: session)
        }
        #endif
        .task { await load() }
        .onAppear {
            openedAt = .now
            analytics.record(.storyOpened, storyID: story.id)
        }
        .onDisappear {
            let dwell = Date.now.timeIntervalSince(openedAt)
            analytics.record(dwell > 6 ? .readWhole : .abandonedHalf, storyID: story.id)
        }
    }

    private var kicker: some View {
        HStack(spacing: 6) {
            Chip(text: s.confidenceWord, style: confidenceChip)
            if s.hasPrimary { Chip(text: "primary source", style: .attributed) }
            if s.extremity == .extraordinary { Chip(text: "extraordinary", style: .extraordinary) }
            Spacer(minLength: 6)
            Button {
                pins.toggle(story.id)
            } label: {
                Image(systemName: pins.isPinned(story.id) ? "bookmark.fill" : "bookmark")
            }
            .tint(pins.isPinned(story.id) ? Palette.confHigh : Palette.muted)
            .accessibilityLabel(pins.isPinned(story.id) ? "Unpin story" : "Pin story")
        }
    }

    private var byline: some View {
        HStack(spacing: 8) {
            if articles.count > 1 {
                Menu {
                    ForEach(articles) { article in
                        Button {
                            selectedArticleID = article.id
                            showTranslated = false
                        } label: {
                            Text(sourceMenuLabel(article))
                        }
                    }
                } label: {
                    Label(selectedArticle?.source ?? "Source", systemImage: "newspaper")
                        .font(.subheadline.weight(.medium))
                }
                .tint(Palette.ink)
            } else {
                Text(selectedArticle?.source ?? "")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Palette.ink)
            }
            if let source = selectedArticle?.source, let rating = feed.rating(for: source) {
                Text("· \(rating.tier)")
                    .font(.caption).foregroundStyle(rating.band.color)
            }
            Spacer(minLength: 0)
            if articleLanguage != "en" {
                Text(articleLanguage.uppercased()).font(.caption2).foregroundStyle(Palette.muted)
            }
        }
    }

    private func sourceMenuLabel(_ article: Article) -> String {
        guard let source = article.source else { return "Unknown source" }
        if let rating = feed.rating(for: source), !rating.coldStart {
            return "\(source) · \(rating.tier)"
        }
        return source
    }

    private var translateButton: some View {
        Button {
            Task { await toggleTranslate() }
        } label: {
            HStack(spacing: 6) {
                if translating { ProgressView() }
                Image(systemName: "character.bubble")
                Text(showTranslated
                     ? "Show original (\(articleLanguage.uppercased()))"
                     : "Translate to \(settings.displayLanguageCode.uppercased())")
            }
        }
        .buttonStyle(.bordered)
        .disabled(translating)
    }

    @ViewBuilder private var articleBody: some View {
        if hasBody {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(Array(displayBody.components(separatedBy: "\n\n").enumerated()), id: \.offset) { _, para in
                    Text(para)
                        .font(.system(.body, design: .serif))
                        .lineSpacing(5)
                        .foregroundStyle(Palette.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        } else {
            SectionCard(title: "In brief · on-device") {
                if summarizing {
                    HStack(spacing: 8) { ProgressView(); Text("Summarising…").foregroundStyle(Palette.muted) }
                } else {
                    Text(summary ?? s.fact).foregroundStyle(Palette.ink)
                }
                Text("Full article text isn't available for this story yet.")
                    .font(.caption2).foregroundStyle(Palette.muted)
            }
        }
    }

    // MARK: Context (veracity & reputation, beneath the article)

    private var contextSections: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Context — how trustworthy is this")
                .font(.caption.weight(.semibold))
                .textCase(.uppercase)
                .foregroundStyle(Palette.muted)
                .padding(.top, 6)

            SectionCard(title: "Confidence") {
                ConfidenceBar(story: s)
                HStack(spacing: 6) {
                    ExtremityChip(extremity: s.extremity)
                    if s.hasPrimary { Chip(text: "primary source", style: .fact) }
                }
            }

            SectionCard(title: "Sources & reputation") {
                ForEach(orderedSources, id: \.self) { name in
                    HStack(alignment: .firstTextBaseline) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(name).font(.subheadline).foregroundStyle(Palette.ink)
                            if let rating = feed.rating(for: name) {
                                Text(rating.tier).font(.caption2).foregroundStyle(rating.band.color)
                            }
                        }
                        Spacer(minLength: 0)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 4)
                    if name != orderedSources.last { Divider().overlay(Palette.line) }
                }
            }

            SectionCard {
                DisclosureGroup {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Independent originators, not spread")
                            .font(.caption).foregroundStyle(Palette.muted)
                        OriginatorList(groups: s.originatorGroups)
                        Text("Claims").font(.caption).foregroundStyle(Palette.muted).padding(.top, 4)
                        ForEach(s.claims) { claim in
                            VStack(alignment: .leading, spacing: 6) {
                                ClaimBadges(claim: claim)
                                Text((showTranslated ? translatedClaims[claim.id] : nil) ?? claim.text)
                                    .font(.subheadline).foregroundStyle(Palette.ink)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.vertical, 6)
                            if claim.id != s.claims.last?.id { Divider().overlay(Palette.line) }
                        }
                    }
                    .padding(.top, 8)
                } label: {
                    Text("Claims & why this confidence")
                        .font(.subheadline.weight(.medium)).foregroundStyle(Palette.ink)
                }
                .tint(Palette.muted)
            }

            deeperSection
            HStack(spacing: 12) {
                feedbackButton(.feedbackUp, icon: "hand.thumbsup")
                feedbackButton(.feedbackDown, icon: "hand.thumbsdown")
                Spacer(minLength: 0)
            }
            commentsLink
        }
    }

    private var deeperSection: some View {
        SectionCard(title: "Go deeper") {
            if let deeper {
                Text(deeper.note).font(.footnote).foregroundStyle(Palette.muted)
                ForEach(deeper.provenance) { p in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(p.source ?? "—").font(.subheadline.weight(.medium))
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
            CommentsView(story: s)
        } label: {
            SectionCard(title: "Comments") {
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

    // MARK: Actions

    private func load() async {
        if full == nil {
            full = try? await feed.loadStory(id: story.id, deeper: false)
        }
        if selectedArticleID == nil { selectedArticleID = defaultArticle?.id }
        await summarize()
    }

    private func summarize() async {
        guard summary == nil else { return }
        summarizing = true
        defer { summarizing = false }
        summary = await FoundationModelsSummarizer().summarize(s)
        analytics.record(.readSummary, storyID: story.id)
    }

    private func toggleTranslate() async {
        if showTranslated { showTranslated = false; return }
        guard let article = selectedArticle else { return }
        if translatedBody[article.id] == nil {
            translating = true
            defer { translating = false }
            translator.preferOnDevice = settings.preferOnDeviceTranslation
            translator.cloud = settings.apiBaseURL.isEmpty
                ? nil
                : URL(string: settings.apiBaseURL).map { CloudTranslator(baseURL: $0) }
            let texts = [article.title ?? s.fact, article.body] + s.claims.map(\.text)
            let out = await translator.translate(
                texts, to: settings.displayLanguageCode, from: article.language
            )
            if out.count == texts.count {
                translatedTitle[article.id] = out[0]
                translatedBody[article.id] = out[1]
                for (index, claim) in s.claims.enumerated() {
                    translatedClaims[claim.id] = out[index + 2]
                }
            }
            analytics.record(.translated, storyID: story.id)
        }
        showTranslated = translatedBody[article.id] != nil
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
