import SwiftUI

struct SettingsView: View {
    @Environment(AppSettings.self) private var settings
    @Environment(FeedStore.self) private var feed
    @Environment(TopicStore.self) private var topics
    @Environment(Analytics.self) private var analytics
    @Environment(\.dismiss) private var dismiss
    @State private var showFeedback = false

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

                Section("Feedback") {
                    Button {
                        showFeedback = true
                    } label: {
                        Label("Send feedback", systemImage: "bubble.left.and.bubble.right")
                    }
                    Text("Tell us what's broken or what you'd like — it goes straight to the team.")
                        .font(.caption2)
                        .foregroundStyle(Palette.muted)
                }

                Section("On-device intelligence") {
                    Label(Intelligence.statusDescription,
                          systemImage: Intelligence.isAvailable ? "checkmark.seal" : "exclamationmark.triangle")
                }

                Section {
                    ForEach(LanguageCatalog.codes, id: \.self) { code in
                        Button {
                            toggleLanguage(code)
                        } label: {
                            HStack(spacing: 8) {
                                Text(AppSettings.languageName(code)).foregroundStyle(Palette.ink)
                                if settings.preferredLanguages.first == code {
                                    Text("primary").font(.caption2).foregroundStyle(Palette.muted)
                                }
                                Spacer(minLength: 0)
                                if settings.preferredLanguages.contains(code) {
                                    Image(systemName: "checkmark").foregroundStyle(Palette.confHigh)
                                }
                            }
                        }
                        .buttonStyle(.plain)
                    }
                } header: {
                    Text("Reading languages")
                } footer: {
                    Text("Headlines and titles not in one of these are translated into "
                         + "\(AppSettings.languageName(settings.displayLanguageCode)) on this device. "
                         + "The first one is your primary language.")
                }

                Section("Translation") {
                    Toggle("Prefer on-device translation", isOn: $settings.preferOnDeviceTranslation)
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
            .sheet(isPresented: $showFeedback) { FeedbackView() }
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

    /// Toggle a reading language. Selection order is preference order (first = primary translation
    /// target); at least one language always stays selected.
    private func toggleLanguage(_ code: String) {
        var langs = settings.preferredLanguages
        if let index = langs.firstIndex(of: code) {
            guard langs.count > 1 else { return }
            langs.remove(at: index)
        } else {
            langs.append(code)
        }
        settings.preferredLanguages = langs
    }
}

/// In-app feedback composer (#210). Free text + a category, posted to the reader's intake
/// (`/api/feedback`, #58) so it enters the triage → review/auto-fix loop. Reusable from a story
/// detail (pass `storyID`); from Settings it's nil.
struct FeedbackView: View {
    @Environment(AppSettings.self) private var settings
    @Environment(\.dismiss) private var dismiss

    var storyID: String? = nil

    @State private var category: FeedbackCategory = .bug
    @State private var text = ""
    @State private var phase: Phase = .editing

    private enum Phase: Equatable { case editing, sending, failed(String) }

    private var canSend: Bool {
        phase != .sending && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Category") {
                    Picker("Category", selection: $category) {
                        ForEach(FeedbackCategory.allCases) { Text($0.label).tag($0) }
                    }
                    .pickerStyle(.menu)
                }
                Section("Your feedback") {
                    TextEditor(text: $text).frame(minHeight: 140)
                }
                if storyID != nil {
                    Label("Attached to the story you're reading", systemImage: "doc.text")
                        .font(.caption)
                        .foregroundStyle(Palette.muted)
                }
                if case .failed(let message) = phase {
                    Label(message, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.red)
                }
                Section {
                    Text("Goes straight to the Maat team — no account or personal info needed.")
                        .font(.caption2)
                        .foregroundStyle(Palette.muted)
                }
            }
            .navigationTitle("Send feedback")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    if phase == .sending {
                        ProgressView()
                    } else {
                        Button("Send") { Task { await send() } }.disabled(!canSend)
                    }
                }
            }
        }
    }

    private func send() async {
        phase = .sending
        // Feedback should reach the team even in bundled-sample mode — fall back to the prod reader.
        let raw = settings.apiBaseURL.isEmpty ? AppSettings.defaultAPIBaseURL : settings.apiBaseURL
        guard let base = URL(string: raw) else {
            phase = .failed("No reader configured.")
            return
        }
        do {
            try await APIFeedbackService(baseURL: base).submit(text: text, category: category, storyID: storyID)
            dismiss()
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }
}
