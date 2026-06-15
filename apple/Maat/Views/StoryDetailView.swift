import SwiftUI

#if canImport(Translation)
import Translation
#endif

// Read-first story detail (BRIEF §1). The fact + on-device summary lead; the confidence read and the
// per-source reputation sit beneath; the claim-level veracity machinery (§5.2–5.6) tucks behind a
// "Why this confidence" disclosure so reading comes first. Translate / go deeper / comment / pin stay.

struct StoryDetailView: View {
    let story: Story

    @Environment(AppSettings.self) private var settings
    @Environment(FeedStore.self) private var feed
    @Environment(Analytics.self) private var analytics
    @Environment(PinStore.self) private var pins

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

    private var orderedSources: [String] {
        var seen = Set<String>()
        return story.originatorGroups
            .sorted { !$0.collapsed && $1.collapsed }  // independent originators first (§5.5)
            .flatMap(\.sources)
            .filter { seen.insert($0).inserted }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                summarySection
                confidenceSection
                sourcesSection
                actionsSection
                deeperSection
                whySection
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
        SectionCard {
            HStack {
                ExtremityChip(extremity: story.extremity)
                Spacer(minLength: 8)
                Button {
                    pins.toggle(story.id)
                } label: {
                    Image(systemName: pins.isPinned(story.id) ? "bookmark.fill" : "bookmark")
                }
                .tint(pins.isPinned(story.id) ? Palette.confHigh : Palette.muted)
                .accessibilityLabel(pins.isPinned(story.id) ? "Unpin story" : "Pin story")
            }
            Text(displayFact)
                .font(.system(.title3, design: .serif).weight(.semibold))
                .foregroundStyle(Palette.ink)
                .fixedSize(horizontal: false, vertical: true)
            Text(orderedSources.joined(separator: " · "))
                .font(.caption)
                .foregroundStyle(Palette.muted)
        }
    }

    private var summarySection: some View {
        SectionCard(title: "Summary") {
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
                Text(Intelligence.statusDescription).font(.caption2).foregroundStyle(Palette.muted)
            }
        }
    }

    private var confidenceSection: some View {
        SectionCard(title: "Confidence") {
            ConfidenceBar(story: story)
            HStack(spacing: 6) {
                ExtremityChip(extremity: story.extremity)
                if story.hasPrimary { Chip(text: "primary source", style: .fact) }
            }
        }
    }

    private var sourcesSection: some View {
        SectionCard(title: "Sources & reputation") {
            ForEach(orderedSources, id: \.self) { name in
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(name).font(.subheadline).foregroundStyle(Palette.ink)
                        if let rating = feed.rating(for: name) {
                            Text(rating.tier).font(.caption2).foregroundStyle(rating.band.color)
                        }
                    }
                    Spacer(minLength: 8)
                    if let rating = feed.rating(for: name) {
                        Text(rating.coldStart ? "—" : "\(rating.score)")
                            .font(.subheadline.weight(.semibold)).monospacedDigit()
                            .foregroundStyle(rating.coldStart ? Palette.muted : rating.band.color)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 4)
                if name != orderedSources.last { Divider().overlay(Palette.line) }
            }
        }
    }

    private var actionsSection: some View {
        SectionCard(title: "This story") {
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

    private var whySection: some View {
        SectionCard {
            DisclosureGroup {
                VStack(alignment: .leading, spacing: 12) {
                    Text("Independent originators, not spread")
                        .font(.caption).foregroundStyle(Palette.muted)
                    OriginatorList(groups: story.originatorGroups)
                    Text("Claims").font(.caption).foregroundStyle(Palette.muted).padding(.top, 4)
                    ForEach(story.claims) { claim in
                        VStack(alignment: .leading, spacing: 6) {
                            ClaimBadges(claim: claim)
                            Text((showTranslated ? translatedClaims[claim.id] : nil) ?? claim.text)
                                .font(.subheadline)
                                .foregroundStyle(Palette.ink)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.vertical, 6)
                        if claim.id != story.claims.last?.id { Divider().overlay(Palette.line) }
                    }
                }
                .padding(.top, 8)
            } label: {
                Text("Why this confidence").font(.subheadline.weight(.medium)).foregroundStyle(Palette.ink)
            }
            .tint(Palette.muted)
        }
    }

    private var commentsLink: some View {
        NavigationLink {
            CommentsView(story: story)
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

    private func summarize() async {
        guard summary == nil else { return }
        summarizing = true
        defer { summarizing = false }
        summary = await FoundationModelsSummarizer().summarize(story)
        analytics.record(.readSummary, storyID: story.id)
    }

    private func toggleTranslate() async {
        if showTranslated { showTranslated = false; return }
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
