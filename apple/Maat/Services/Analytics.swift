import Foundation
import Observation

// Engagement capture (#57, PLAN §6). Collection-only: we gather signals for a *named purpose* now and
// learn what they mean later — we do NOT pre-decide their meaning or route them anywhere.
//
// Two lanes, by construction:
//   • Lane 1 (individual): per-event signals, including the story id — these STAY ON DEVICE. In-memory
//     only; never persisted, never transmitted.
//   • Lane 2 (aggregate): anonymised counts — no ids, no text. This is the *only* thing that could ever
//     be edge-aggregated and sent upstream. Transmission is disabled here (collection-only).

enum EngagementSignal: String, Codable, Sendable, CaseIterable {
    case storyOpened
    case readSummary
    case readWhole
    case abandonedHalf
    case commented
    case feedbackUp
    case feedbackDown
    case translated
    case wentDeeper
    case searched
}

struct EngagementEvent: Identifiable, Sendable {
    let id = UUID()
    var signal: EngagementSignal
    var storyID: String?
    var at: Date
}

@MainActor
@Observable
final class Analytics {
    /// Lane 1 — individual, on-device only. Rolling buffer; never leaves the phone.
    private(set) var events: [EngagementEvent] = []

    /// Lane 2 — anonymised aggregate counts. The only lane eligible for edge upload (disabled now).
    private(set) var aggregate: [String: Int] = [:]

    /// Hard switch: nothing is transmitted in P6 (capture for a named purpose or don't capture it).
    let transmissionEnabled = false

    private let maxEvents = 500
    private let defaults: UserDefaults
    private let aggregateKey = "maat.analytics.aggregate"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        if let saved = defaults.dictionary(forKey: aggregateKey) as? [String: Int] {
            aggregate = saved
        }
    }

    func record(_ signal: EngagementSignal, storyID: String? = nil) {
        // Lane 1: keep the id-bearing event in memory only.
        events.append(EngagementEvent(signal: signal, storyID: storyID, at: .now))
        if events.count > maxEvents { events.removeFirst(events.count - maxEvents) }

        // Lane 2: bump the anonymised count (no id, no text) and persist that lane only.
        aggregate[signal.rawValue, default: 0] += 1
        defaults.set(aggregate, forKey: aggregateKey)
    }

    /// The anonymised, aggregated payload that edge-aggregation would hand upstream — counts only.
    /// Returned for inspection (Settings) and future transmission; not sent while disabled.
    func anonymisedRollup() -> [String: Int] { aggregate }

    func reset() {
        events.removeAll()
        aggregate.removeAll()
        defaults.removeObject(forKey: aggregateKey)
    }
}
