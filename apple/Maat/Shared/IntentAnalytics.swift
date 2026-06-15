import Foundation

// Extension-safe engagement counting (#57, #83). The app's `Analytics` is in-memory @MainActor state
// the App Intents *extension* can't reach across the process boundary, so a launch-free intent records
// only Lane 2 — the anonymised aggregate counts, which are already UserDefaults-backed and so are
// shared between the app and the extension. No ids, no text leave the device; Lane 1 (the id-bearing
// per-event buffer) stays in the app process only, by construction.
enum IntentAnalytics {
    private static let aggregateKey = "maat.analytics.aggregate"

    /// Bump the anonymised count for a signal (counts only — matches `Analytics.record`'s Lane 2).
    static func record(_ signal: EngagementSignal) {
        let defaults = UserDefaults.standard
        var aggregate = (defaults.dictionary(forKey: aggregateKey) as? [String: Int]) ?? [:]
        aggregate[signal.rawValue, default: 0] += 1
        defaults.set(aggregate, forKey: aggregateKey)
    }
}

// Add a topic from the launch-free "add topic" intent (#83). Topics live in the same UserDefaults key
// `TopicStore` reads (`maat.topics`), so appending here lands in the reader's topic list; the app
// re-ranks the feed against the updated topics on its next bootstrap. On-device, per-user (PLAN §6).
enum IntentTopics {
    private static let key = "maat.topics"

    /// Append a topic if it's non-empty and not already present; returns whether it was added.
    @discardableResult
    static func add(_ text: String) -> Bool {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return false }
        let defaults = UserDefaults.standard
        var topics = defaults.stringArray(forKey: key) ?? ["world politics", "AI"]
        guard !topics.contains(trimmed) else { return false }
        topics.append(trimmed)
        defaults.set(topics, forKey: key)
        return true
    }
}
