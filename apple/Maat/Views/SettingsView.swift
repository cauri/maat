import SwiftUI

struct SettingsView: View {
    @Environment(AppSettings.self) private var settings
    @Environment(FeedStore.self) private var feed
    @Environment(TopicStore.self) private var topics
    @Environment(Analytics.self) private var analytics
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        @Bindable var settings = settings
        NavigationStack {
            Form {
                Section("Reader") {
                    TextField("API base URL (blank = bundled sample)", text: $settings.apiBaseURL)
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        #endif
                    Button("Reload feed") {
                        Task { await reload() }
                    }
                    if feed.usingFallback {
                        Label("Using bundled sample feed", systemImage: "internaldrive")
                            .foregroundStyle(Palette.muted)
                    }
                }

                Section("On-device intelligence") {
                    Label(Intelligence.statusDescription,
                          systemImage: Intelligence.isAvailable ? "checkmark.seal" : "exclamationmark.triangle")
                }

                Section("Translation") {
                    Toggle("Prefer on-device translation", isOn: $settings.preferOnDeviceTranslation)
                    TextField("Display language code", text: $settings.displayLanguageCode)
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        #endif
                }

                Section("Privacy · captured signals") {
                    let rollup = analytics.anonymisedRollup().sorted { $0.key < $1.key }
                    if rollup.isEmpty {
                        Text("Nothing captured yet.").foregroundStyle(Palette.muted)
                    }
                    ForEach(rollup, id: \.key) { key, value in
                        HStack {
                            Text(key)
                            Spacer()
                            Text("\(value)").foregroundStyle(Palette.muted).monospacedDigit()
                        }
                    }
                    Text(analytics.transmissionEnabled
                         ? "Aggregated, anonymised counts may be sent upstream."
                         : "Nothing is transmitted — collection only. Individual signals never leave this device.")
                        .font(.caption2)
                        .foregroundStyle(Palette.muted)
                    Button("Reset captured signals", role: .destructive) {
                        analytics.reset()
                    }
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }

    private func reload() async {
        feed.setService(settings.makeFeedService())
        await feed.refresh()
        await feed.applyRerank(FoundationModelsReranker(), topics: topics.topics)
    }
}
