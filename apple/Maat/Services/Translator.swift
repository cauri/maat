import Foundation
import Observation

#if canImport(Translation)
import Translation
#endif

// Translate-for-display only (#54, PLAN §4 — never score a translation). Order of preference:
// on-device (Apple Translation framework, free/private/offline) → cloud fallback (POST /api/translate,
// for pairs the device can't do) → identity (show the original). On-device translation is view-bound
// (it needs a live `TranslationSession` from `.translationTask`), so it lives in `TranslationController`
// which a hosting view drives; the cloud/identity legs are plain `Translator`s.

protocol Translator: Sendable {
    func translate(_ text: String, to target: String, from source: String?) async throws -> String
}

struct IdentityTranslator: Translator {
    func translate(_ text: String, to target: String, from source: String?) async throws -> String {
        text
    }
}

/// Cloud fallback — routes to the reader's /api/translate (a stub today; real impl goes through the
/// Source/Effect seam server-side). Used only when the on-device pair is unavailable.
struct CloudTranslator: Translator {
    var baseURL: URL
    var session: URLSession = .shared

    private struct Req: Encodable { var text: String; var target: String; var source: String? }
    private struct Resp: Decodable { var translated: String }

    func translate(_ text: String, to target: String, from source: String?) async throws -> String {
        var request = URLRequest(url: baseURL.appending(path: "api/translate"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Req(text: text, target: target, source: source))
        let (data, resp) = try await session.data(for: request)
        if let http = resp as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw FeedError.badResponse(http.statusCode)
        }
        return try JSONDecoder().decode(Resp.self, from: data).translated
    }
}

/// Drives on-device translation through `.translationTask`, with cloud → identity fallback. A view
/// must host `.translationTask(controller.configuration) { await controller.fulfill(using: $0) }`.
@MainActor
@Observable
final class TranslationController {
    var preferOnDevice: Bool = true
    /// Set when an API base URL is configured; nil → no cloud leg, fall straight through to identity.
    var cloud: CloudTranslator?

    #if canImport(Translation)
    var configuration: TranslationSession.Configuration?
    #endif

    private var continuation: CheckedContinuation<[String]?, Never>?
    private var pendingTexts: [String] = []

    /// Translate a batch in order. Never throws — worst case returns the originals.
    func translate(_ texts: [String], to target: String, from source: String?) async -> [String] {
        guard !texts.isEmpty else { return texts }

        #if canImport(Translation)
        if preferOnDevice, #available(iOS 18, macOS 15, *) {
            if let onDevice = await translateOnDevice(texts, to: target, from: source) {
                return onDevice
            }
        }
        #endif

        if let cloud {
            var out: [String] = []
            for text in texts {
                let translated = (try? await cloud.translate(text, to: target, from: source)) ?? text
                out.append(translated)
            }
            return out
        }

        return texts // identity
    }

    /// The texts a live session should translate, or nil if there's no outstanding request.
    private func takePending() -> [String]? {
        continuation == nil ? nil : pendingTexts
    }

    /// Resume the awaiting caller with the on-device result (nil → it should fall back).
    private func complete(with result: [String]?) {
        let cont = continuation
        continuation = nil
        #if canImport(Translation)
        configuration = nil
        #endif
        cont?.resume(returning: result)
    }

    #if canImport(Translation)
    @available(iOS 18, macOS 15, *)
    private func translateOnDevice(_ texts: [String], to target: String, from source: String?) async -> [String]? {
        pendingTexts = texts
        return await withCheckedContinuation { (cont: CheckedContinuation<[String]?, Never>) in
            continuation = cont
            configuration = TranslationSession.Configuration(
                source: source.map { Locale.Language(identifier: $0) },
                target: Locale.Language(identifier: target)
            )
        }
    }

    /// Called from the hosting view's `.translationTask` closure once a session is live. `nonisolated`
    /// so the (non-Sendable) session isn't pinned to the main actor — we only hop back for state.
    @available(iOS 18, macOS 15, *)
    nonisolated func fulfill(using session: sending TranslationSession) async {
        guard let texts = await takePending() else { return }
        var out: [String] = []
        do {
            for text in texts {
                out.append(try await session.translate(text).targetText)
            }
            await complete(with: out)
        } catch {
            await complete(with: nil) // on-device couldn't do it → caller falls back
        }
    }
    #endif
}

/// Translates Today-feed headlines (the corroborated facts) into the reader's PRIMARY language on
/// device, skipping any story already in a language they read (#54). View-bound like
/// `TranslationController`: the feed must host
/// `.translationTask(titler.controller.configuration) { await titler.controller.fulfill(using: $0) }`.
@MainActor
@Observable
final class FeedTitleTranslator {
    private(set) var titles: [String: String] = [:]   // story id -> headline in the primary language
    let controller = TranslationController()
    private var signature = ""
    private var translating = false

    /// The headline to show for a story: its primary-language translation when we have one, else the
    /// original fact (shown while a translation is still resolving, or when the story is already read).
    func headline(for story: Story) -> String { titles[story.id] ?? story.fact }

    /// Translate not-yet-done, non-preferred-language headlines into `target`. Idempotent; safe to call
    /// on every feed/preference change. Changing the preferred set clears the cache so it re-translates
    /// (a story now in a read language reverts to its original). `reads` decides which stories to skip.
    func sync(_ stories: [Story], target: String, preferredSignature: String,
              reads: (String) -> Bool, preferOnDevice: Bool, cloud: CloudTranslator?) async {
        if preferredSignature != signature {
            signature = preferredSignature
            titles.removeAll()
        }
        guard !translating else { return }
        let todo = stories.filter { titles[$0.id] == nil && !reads($0.primaryLanguage) }
        guard !todo.isEmpty else { return }
        translating = true
        defer { translating = false }
        controller.preferOnDevice = preferOnDevice
        controller.cloud = cloud
        let out = await controller.translate(todo.map(\.fact), to: target, from: nil)
        guard out.count == todo.count else { return }
        for (story, translated) in zip(todo, out) where !translated.isEmpty {
            titles[story.id] = translated
        }
    }
}
