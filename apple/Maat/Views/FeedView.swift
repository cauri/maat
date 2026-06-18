import SwiftUI

struct FeedView: View {
    @Environment(FeedStore.self) private var feed
    @Environment(TopicStore.self) private var topics
    @Environment(AppRouter.self) private var router
    @Environment(AppSettings.self) private var settings
    @State private var showSettings = false
    @State private var titler = FeedTitleTranslator()

    var body: some View {
        @Bindable var router = router
        NavigationStack(path: $router.feedPath) {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if feed.servingCache, let date = feed.cacheDate {
                        FallbackBanner(
                            message: "Offline — showing your last update from \(date.formatted(.relative(presentation: .numeric)))",
                            icon: "clock.arrow.circlepath"
                        ).padding(.bottom, 14)
                    } else if feed.usingFallback, let error = feed.error {
                        FallbackBanner(message: error).padding(.bottom, 14)
                    }

                    let stories = feed.displayStories
                    if let lead = stories.first {
                        NavigationLink(value: lead) {
                            LeadStoryCard(story: lead, headline: titler.headline(for: lead))
                        }
                        .buttonStyle(.plain)

                        if stories.count > 1 {
                            Text("More stories")
                                .font(.caption.weight(.semibold))
                                .textCase(.uppercase)
                                .foregroundStyle(Palette.muted)
                                .padding(.top, 20)
                        }
                        ForEach(stories.dropFirst()) { story in
                            NavigationLink(value: story) {
                                StoryRow(story: story, headline: titler.headline(for: story))
                            }
                            .buttonStyle(.plain)
                            Divider().overlay(Palette.line)
                        }
                    } else if !feed.isLoading {
                        ContentUnavailableView(
                            "No stories yet",
                            systemImage: "newspaper",
                            description: Text("Start the agents and ingest a corpus, or point Settings at a reader.")
                        )
                        .padding(.top, 60)
                    }
                }
                .padding()
            }
            .background(Palette.bg)
            .navigationTitle("Today")
            .navigationDestination(for: Story.self) { StoryDetailView(story: $0) }
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { showSettings = true } label: {
                        Image(systemName: "gearshape")
                    }
                    .accessibilityLabel("Settings")
                }
            }
            .sheet(isPresented: $showSettings) { SettingsView() }
            .overlay {
                if feed.isLoading, feed.stories.isEmpty { ProgressView() }
            }
            .refreshable {
                await feed.refresh()
                await feed.applyRerank(FoundationModelsReranker(), topics: topics.topics)
                await feed.refreshSources()
            }
            #if canImport(Translation)
            .translationTask(titler.controller.configuration) { session in
                await titler.controller.fulfill(using: session)
            }
            #endif
            .task(id: titleSyncKey) { await syncTitles() }
        }
    }

    /// Re-key the on-device headline translation when the feed or the reader's languages change.
    private var titleSyncKey: String {
        feed.displayStories.map(\.id).joined(separator: ",") + "|"
            + settings.preferredLanguages.joined(separator: ",")
    }

    private func syncTitles() async {
        let cloud = settings.apiBaseURL.isEmpty
            ? nil
            : URL(string: settings.apiBaseURL).map { CloudTranslator(baseURL: $0) }
        await titler.sync(
            feed.displayStories,
            target: settings.displayLanguageCode,
            preferredSignature: settings.preferredLanguages.joined(separator: ","),
            reads: { settings.reads($0) },
            preferOnDevice: settings.preferOnDeviceTranslation,
            cloud: cloud
        )
    }
}

struct FallbackBanner: View {
    var message: String
    var icon: String = "internaldrive"
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon)
            Text(message).font(.footnote)
            Spacer(minLength: 0)
        }
        .foregroundStyle(Palette.confMid)
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.wireBg, in: RoundedRectangle(cornerRadius: 10))
    }
}
