import Foundation
import Observation

// Per-user, on-device preferences (PLAN §6: topics + personalisation stay on the phone). Persisted in
// UserDefaults; nothing here leaves the device.

@MainActor
@Observable
final class AppSettings {
    /// Empty → use the bundled fixture. Otherwise the base URL of a Maat reader (e.g. the Hetzner box).
    var apiBaseURL: String {
        didSet { defaults.set(apiBaseURL, forKey: Keys.api) }
    }

    /// Translate on-device first; only fall back to the cloud endpoint when a pair is unavailable (#54).
    var preferOnDeviceTranslation: Bool {
        didSet { defaults.set(preferOnDeviceTranslation, forKey: Keys.onDevice) }
    }

    /// Target language for display translation. Defaults to the device language.
    var displayLanguageCode: String {
        didSet { defaults.set(displayLanguageCode, forKey: Keys.lang) }
    }

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        apiBaseURL = defaults.string(forKey: Keys.api) ?? ""
        preferOnDeviceTranslation = defaults.object(forKey: Keys.onDevice) as? Bool ?? true
        displayLanguageCode = defaults.string(forKey: Keys.lang)
            ?? Locale.current.language.languageCode?.identifier ?? "en"
    }

    func makeFeedService() -> FeedService {
        guard !apiBaseURL.isEmpty, let url = URL(string: apiBaseURL) else {
            return FixtureFeedService()
        }
        return APIFeedService(baseURL: url)
    }

    private enum Keys {
        static let api = "maat.apiBaseURL"
        static let onDevice = "maat.preferOnDeviceTranslation"
        static let lang = "maat.displayLanguageCode"
    }
}

/// The reader's natural-language topics (§6 — per-user, on-device). These steer the on-device
/// re-rank of the served feed (#53); they are never sent to the server.
@MainActor
@Observable
final class TopicStore {
    var topics: [String] {
        didSet { defaults.set(topics, forKey: key) }
    }

    private let defaults: UserDefaults
    private let key = "maat.topics"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        topics = defaults.stringArray(forKey: key) ?? ["world politics", "AI"]
    }

    func add(_ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty, !topics.contains(t) else { return }
        topics.append(t)
    }

    func remove(at offsets: IndexSet) {
        topics.remove(atOffsets: offsets)
    }
}

/// Pinned stories the reader follows (BRIEF §1 — pin a story to follow it). On-device, per-user.
@MainActor
@Observable
final class PinStore {
    var pinned: [String] {
        didSet { defaults.set(pinned, forKey: key) }
    }

    private let defaults: UserDefaults
    private let key = "maat.pins"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        pinned = defaults.stringArray(forKey: key) ?? []
    }

    func isPinned(_ id: String) -> Bool { pinned.contains(id) }

    func toggle(_ id: String) {
        if let index = pinned.firstIndex(of: id) {
            pinned.remove(at: index)
        } else {
            pinned.append(id)
        }
    }
}
