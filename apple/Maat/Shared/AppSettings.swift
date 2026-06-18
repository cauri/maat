import Foundation
import Observation

// Per-user, on-device preferences (PLAN §6: topics + personalisation stay on the phone). Persisted in
// UserDefaults; nothing here leaves the device.

@MainActor
@Observable
final class AppSettings {
    /// The deployed Maat reader the app talks to by default (the Hetzner box, over HTTPS). Overridable
    /// in Settings; cleared → the bundled fixture (offline fallback). The feed store also falls back to
    /// the fixture automatically if the server is unreachable.
    ///
    /// Debug/Release split (#196): Release ships pointed at the production box; Debug uses the same
    /// default but is the single place to point a development build at a local/staging reader (e.g.
    /// "http://localhost:8000") without touching Release — change the Debug line below. An unreachable
    /// dev URL just falls back to the bundled fixture, so this is safe to repoint.
    /// `nonisolated` so the (nonisolated) `IntentDataSource` in the App Intents extension can read this
    /// default reader URL; it's an immutable constant, so it's safe outside the main actor.
    #if DEBUG
    nonisolated static let defaultAPIBaseURL = "https://api.maat.press"
    #else
    nonisolated static let defaultAPIBaseURL = "https://api.maat.press"
    #endif

    /// Base URL of the Maat reader. Empty → bundled fixture. Defaults to `defaultAPIBaseURL`.
    var apiBaseURL: String {
        didSet { defaults.set(apiBaseURL, forKey: Keys.api) }
    }

    /// Translate on-device first; only fall back to the cloud endpoint when a pair is unavailable (#54).
    var preferOnDeviceTranslation: Bool {
        didSet { defaults.set(preferOnDeviceTranslation, forKey: Keys.onDevice) }
    }

    /// Reading languages in preference order (PLAN §6 — per-user, on-device). The FIRST is the
    /// translation TARGET for display; a title already in ANY of these is shown as-is, never
    /// translated (#54). Persisted; nothing here leaves the device.
    var preferredLanguages: [String] {
        didSet { defaults.set(preferredLanguages, forKey: Keys.langs) }
    }

    /// Primary reading language — the target every non-preferred title/headline is translated into.
    var displayLanguageCode: String { preferredLanguages.first ?? "en" }

    /// True when `language` ("fr", "en-GB", "pt-BR", …) is one the reader already reads, so a title in
    /// it is left in its original language rather than translated.
    func reads(_ language: String) -> Bool {
        let code = AppSettings.baseCode(language)
        return !code.isEmpty && preferredLanguages.contains { AppSettings.baseCode($0) == code }
    }

    /// Base ISO code, region/script dropped: "en-GB" -> "en", "pt-BR" -> "pt".
    nonisolated static func baseCode(_ s: String) -> String {
        String(s.lowercased().split(separator: "-").first ?? "")
    }

    /// Full localised language name in the reader's own locale: "en" -> "English", "ja" -> "Japanese".
    nonisolated static func languageName(_ code: String) -> String {
        Locale.current.localizedString(forLanguageCode: baseCode(code))?.localizedCapitalized
            ?? code.uppercased()
    }

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        apiBaseURL = defaults.string(forKey: Keys.api) ?? Self.defaultAPIBaseURL
        preferOnDeviceTranslation = defaults.object(forKey: Keys.onDevice) as? Bool ?? true
        let device = Locale.current.language.languageCode?.identifier ?? "en"
        // Migrate the pre-#54 single `displayLanguageCode` into the ordered list on first launch.
        preferredLanguages = defaults.stringArray(forKey: Keys.langs)
            ?? [defaults.string(forKey: Keys.lang) ?? device]
    }

    func makeFeedService() -> FeedService {
        guard !apiBaseURL.isEmpty, let url = URL(string: apiBaseURL) else {
            return FixtureFeedService()
        }
        return APIFeedService(baseURL: url)
    }

    /// URL for an article's lead image, served through the reader's privacy-preserving proxy (#1):
    /// the client passes the article *id*, never the origin URL, so the publisher never sees the
    /// reader's users. nil in fixture mode (no server) or when the base URL is invalid.
    func imageURL(articleID: String) -> URL? {
        guard !apiBaseURL.isEmpty, let base = URL(string: apiBaseURL) else { return nil }
        var comps = URLComponents(url: base.appending(path: "api/v2/image"),
                                  resolvingAgainstBaseURL: false)
        comps?.queryItems = [URLQueryItem(name: "article", value: articleID)]
        return comps?.url
    }

    /// URL for an outlet's favicon, served through the box's proxy (#1) so the device never loads a
    /// third-party image host: the client passes the bare domain; the box fetches + caches it. nil in
    /// fixture mode or when the base URL is invalid (the row then shows a drawn monogram).
    func sourceIconURL(domain: String) -> URL? {
        guard !apiBaseURL.isEmpty, let base = URL(string: apiBaseURL) else { return nil }
        var comps = URLComponents(url: base.appending(path: "api/v2/source-icon"),
                                  resolvingAgainstBaseURL: false)
        comps?.queryItems = [URLQueryItem(name: "d", value: domain)]
        return comps?.url
    }

    private enum Keys {
        static let api = "maat.apiBaseURL"
        static let onDevice = "maat.preferOnDeviceTranslation"
        static let lang = "maat.displayLanguageCode"        // legacy (single) — migrated to `langs`
        static let langs = "maat.preferredLanguages"
    }
}

/// The reading languages offered in Settings (#54). A broad curated set spanning Maat's locales (the
/// on-device translator handles any pair the OS has a model for); each shown by full localised name.
enum LanguageCatalog {
    static let codes: [String] = [
        "en", "es", "fr", "de", "it", "pt", "nl", "ru", "uk", "pl", "tr",
        "ar", "fa", "he", "hi", "ur", "zh", "ja", "ko", "id", "vi", "th", "sw",
    ]
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
        // Foundation-only (no SwiftUI `remove(atOffsets:)`): this type compiles into the App Intents
        // extension too (Maat/Shared), which doesn't link SwiftUI. Remove high→low so indices stay valid.
        for index in offsets.sorted(by: >) where topics.indices.contains(index) {
            topics.remove(at: index)
        }
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
