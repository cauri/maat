# Maat — Apple client (P6)

The SwiftUI universal app (iPhone + Mac) for Maat — Tier 2 (on-device) and Tier 3 (PCC) of the
architecture in [`../PLAN.md`](../PLAN.md) §2.1 / §8. The **story** (a corroboration cluster, §5.5) is
the unit: every story carries a confidence read (§5.6–5.7), its independent-originator collapse, and
the claims that compose it. Mirrors the web reader's visual language so the two read as one product.

> Status: P6 scaffold — all six stories (#52–#57) implemented and verified building on the iOS 27 SDK
> and running on the iOS 27 simulator. See [`../DECISIONS.md`](../DECISIONS.md) D23.

## Build & run

The Xcode project is **generated** from `project.yml` ([XcodeGen](https://github.com/yonsm/XcodeGen)) —
it is gitignored, regenerate it after pulling:

```sh
brew install xcodegen          # once
cd apple
xcodegen generate              # writes Maat.xcodeproj
open Maat.xcodeproj            # ⌘R to run (iPhone 17 sim or My Mac)
```

Command-line build (Xcode 27 for the iOS 27 SDK; the deployment floor is 26.0):

```sh
# iOS 27 simulator
DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer \
  xcodebuild -project Maat.xcodeproj -scheme Maat \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro,OS=27.0' build

# Mac
xcodebuild -project Maat.xcodeproj -scheme Maat -destination 'platform=macOS' build
```

## Where the data comes from

By default the app loads a **bundled fixture** (`Maat/Resources/feed.fixture.json`, derived from
`../corpus/`) so it builds, previews, and runs with no backend. Point it at a live reader in
**Settings → Reader → API base URL** (e.g. the Hetzner box); blank = fixture.

The live API is stubbed on the FastAPI reader (`../python/maat/web/app.py`, P5 #48 minimal):

| Endpoint | Purpose |
|---|---|
| `GET /api/feed` | stories (clusters) + claims + labels + confidence + originators + extremity |
| `GET /api/story/{id}?deeper=1` | one story; `deeper=1` adds the Tier-3 provenance expansion (#56) |
| `POST /api/translate` | cloud translation fallback for §4 (stub; on-device is the primary path) |

The Swift `Story`/`Claim` models mirror this JSON exactly (decoded `.convertFromSnakeCase`).

## Layout

```
Maat/
  App/        MaatApp, RootView (tabs), AppSettings + TopicStore (on-device prefs)
  Models/     Story / Claim / OriginatorGroup / Deeper (API), Comment (@Model)
  Services/   FeedService (API + fixture + store), Reranker, Summarizer, SemanticSearch,
              Translator, Analytics, Intelligence (Foundation Models availability)
  Views/      Feed, StoryDetail, Search, Topics, Comments, Settings + components
  Theme/      Palette + chip styles (mirrors the web reader)
  Resources/  feed.fixture.json, Assets.xcassets
```

## Story map (#52–#57)

- **#52 Universal app** — `FeedView` (confidence bar, originator collapse, extremity), `StoryDetailView`
  (claim veracity chips: voice / fact·projection / synthesis / headline), tabbed shell.
- **#53 On-device intelligence** — `Reranker` (re-rank the served feed against your NL topics),
  `Summarizer` (summarise-to-taste), `SemanticSearch` (NaturalLanguage embeddings). Foundation Models
  when available; deterministic fallbacks otherwise.
- **#54 Translation** — `TranslationController` drives Apple's on-device `Translation` framework by
  default, with cloud (`/api/translate`) → identity fallback. Translate-for-display only (§4 — never
  score a translation).
- **#55 Comments (local)** — `Comment` in SwiftData; text + story + timestamp; never leaves the device.
- **#56 Tier-3 "go deeper"** — escalates to `/api/story/{id}?deeper=1`; the server/PCC boundary is
  stubbed (PLAN §11 — PCC developer surface verified at P6).
- **#57 Edge-aggregated analytics** — `Analytics`: two lanes — individual signals stay on-device;
  anonymised counts are the only edge-aggregation lane. Collection-only (no transmission yet).

### App Intents — Siri / Shortcuts / Spotlight / other apps (#80)

`Maat/Intents/` exposes the app's features to the system via the **App Intents** framework, so Siri,
the Shortcuts app, Spotlight, the Action button, and other apps' automations can drive Maat — not just
the in-app UI:

- **`StoryEntity`** (+ `StoryEntityQuery` / `EntityStringQuery`) — a story as a system entity; the
  string query reuses on-device `SemanticSearch`.
- **Intents** — Open Feed, Search Maat, Top Story (speaks an on-device summary), Show a Story, Add a
  Topic, Go Deeper. UI-opening intents route through `AppRouter`; read-only ones return a value +
  spoken `IntentDialog`. Each records an engagement signal (#57).
- **`MaatShortcuts`** (`AppShortcutsProvider`) — zero-setup spoken phrases ("What's the top story on
  Maat", "Search Maat", "Open my Maat feed", "Add a topic to Maat").

Intents and the UI share one source of truth (`MaatCore.shared`, `@MainActor`), so an intent that adds
a topic re-ranks the same feed the UI shows. Verified: the four actions appear under **Maat** in the
iOS 27 Shortcuts app. Follow-up: `IndexedEntity` Spotlight donation, and an App Intents extension so
intents run without launching the app.

## Known constraints / follow-ups

- **Swift 5 language mode (target-wide, temporary).** The iOS 26/27 `Translation` framework's
  `TranslationSession` (non-`Sendable` + `@concurrent translate`) can't be driven from the main-actor
  `.translationTask` closure under Swift 6 strict concurrency on this SDK. The rest of the code is
  Swift-6-clean. Candidate fix: isolate the Translation glue into its own module and keep the app in
  Swift 6. (D23)
- **On-device model needs a real device.** Foundation Models and Apple Translation are unavailable on
  the simulator; the app degrades to fallbacks there (verified). Run on an Apple-Intelligence device
  with the language packs installed to exercise the real paths.
- **`DRAFT` prompts.** The `Summarizer` / `Reranker` instructions fed to Foundation Models are marked
  `DRAFT — review with cauri` (in-platform agent prompts; D22/D23). Do not finalise without review.
- **No app icon art yet** — the asset catalog has the slots and an accent colour; artwork is TODO.
