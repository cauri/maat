# URL → clean article content: research + recommended architecture

*Deep-research synthesis, 2026-07-09. Question: how should Maat turn an arbitrary web URL into
clean, complete article content — reliably, fast, at scale? General website extraction first,
news as the subset. Findings below were adversarially verified (3-vote refutation panel) unless
marked otherwise. Trigger: the Le Pen / Yahoo analysis (P14) — bot-walled publishers returned
empty bodies and live blogs returned 30k chars of noise, starving live corroboration.*

---

## 1. What the evidence says

### 1.1 The extraction library was never the problem — trafilatura is the right primary

Every credible benchmark puts trafilatura at or near the top of open-source main-content
extraction:

- **ScrapingHub/Zyte article-extraction benchmark** (181 human-annotated news pages, the
  canonical head-to-head): trafilatura 2.0.0 scores **F1 0.958** (precision 0.938, recall 0.978)
  — the best Python library; its Rust port rs_trafilatura reaches 0.970.
  [github.com/scrapinghub/article-extraction-benchmark] (verified 3-0)
- **Independent US-gov evaluation** (OSTI, 7 libraries on the same corpus): trafilatura best
  mean F1 0.937 and precision 0.978; readability-lxml second (F1 0.914, best recall 0.929).
  Trafilatura's *precision* advantage is statistically significant (p<0.05) — it leaves
  measurably less boilerplate, which is exactly what our downstream LLM claim extraction needs.
  [osti.gov/servlets/purl/2429881] (verified 3-0)
- **Largest reproduction study** (Bevendorff et al., SIGIR 2023; 14 extractors, 3,985 pages,
  8 datasets): trafilatura best overall mean (macro F1 0.883); readability-lxml most *robust*
  (median 0.970, lowest spread). [dl.acm.org/doi/10.1145/3539618.3591920] (verified 3-0)
- Production scale: the Infini-News pipeline extracted **1.36B articles across 1,172 languages**
  from Common Crawl News with trafilatura. (source-verified, single vote — rate-limited panel)

Two findings that shape the architecture more than any single ranking:

- **No single extractor wins everywhere — performance is genre-dependent** (verified 3-0).
  Standard articles are near-solved; non-article shapes collapse (forums 0.52–0.79,
  collections 0.42–0.71). Live blogs are precisely such a shape — this is our BBC/Le Monde
  failure, and no generic library fixes it.
- **Majority-vote ensembles beat every individual extractor** (weighted
  readability+trafilatura ensemble: mean F1 0.899 vs 0.883 best-single), and extraction runs in
  10–100ms, so running two extractors and comparing is effectively free. A cheap logistic
  classifier on tag counts predicts page complexity at ~80% accuracy — routing is viable.

### 1.2 Don't use neural/LLM models to extract content per-page

Verified 3-0: neural extractors (ReaderLM-v2 1.5B, MinerU-HTML) **underperform the best
heuristics on accuracy** while costing 1.5–10+ s/page on an A100, vs <100ms on CPU for
heuristics. The SIGIR study is blunter: large neural models "perform surprisingly badly",
especially on the complex pages they were designed for. LLM-per-page extraction is the wrong
tool; the LLM belongs at claim extraction (where it already works) and at *schema generation*
(below), not boilerplate removal.

### 1.3 Per-site parsers are the quality ceiling — and the pattern is now automatable

- **Fundus** (hand-crafted per-publisher Python parsers, MIT): F1 **97.69 vs trafilatura's
  89.81** on its 16-publisher benchmark, with far lower variance — and every generic library
  tested fails badly (F1 <60) on at least one publisher. Coverage is the cost: only ~40
  publishers. (source-verified)
- **crawl4ai's `generate_schema`** ships the exact "LLM learns the site once" pattern cauri
  proposed: an LLM produces a site-specific CSS/XPath schema from one or more sample pages,
  then the schema executes deterministically forever — no per-page inference. Multiple samples
  → stable non-positional selectors. (source-verified)
- **Self-healing discipline** (production report): LLM as *proposer*, never decider —
  candidate selectors are validated deterministically against live HTML before promotion;
  repeated failure escalates to a human instead of guessing. Silent selector rot is the failure
  mode to engineer against; detection is the expensive part (~2h) not the fix (~5min).
- Commercial vendors converge on the same shape: Diffbot (CV+NLP page-type classification →
  specialized extractors, custom rules as escape hatch — verified 3-0), Zyte (per-page-type ML
  models retrained a few times a year, "self-healing" via retraining — verified 3-0).

### 1.4 Fetching is the actual bottleneck — and our fetch stack is naive

Modern anti-bot systems detect at four layers: **TLS fingerprint, browser properties,
behavioral signals, IP reputation**. Consequences for us:

- `trafilatura.fetch_url` / default httpx have a non-browser TLS handshake → flagged before
  headers are even read. **curl-cffi / curl-impersonate** mimics Chrome's TLS+HTTP/2
  fingerprint at HTTP-client speed — the cheapest single upgrade.
- Sites running JS challenges need a real browser; 2026 consensus stealth options:
  **Camoufox** (hardened Firefox fork) and **nodriver** (CDP, no `navigator.webdriver`);
  vanilla playwright-stealth is considered inadequate.
- **Our Hetzner box is a datacenter IP — low trust by construction.** Cloudflare-class systems
  score residential/ISP IPs high and cloud IPs low; residential proxy egress (or a managed
  unblocker that bundles it) is part of any serious answer for hostile publishers.
- Managed unblocker APIs (Zyte API, Scrapfly ~98% claimed success vs 20 anti-bot vendors,
  pay-on-success) bundle TLS spoofing + browser rendering + residential IPs + retries in one
  call. Zyte API additionally returns *extracted article JSON* from the same request
  (`extractFrom: httpResponseBody | browserHtml` — the cheap-first/expensive-last ladder as a
  vendor parameter). (verified 3-0)
- This is an arms race (per-domain dynamic trust scores, behavioral signals) — budget for the
  escalation tier to be *maintained*, i.e. lean managed rather than self-built for the hardest
  sites.

### 1.5 Structured data is a first-class extraction source — and the live-blog fix

- **GDELT — a production aggregator at global scale — harvests JSON-LD from every article it
  crawls** and publishes it as a dataset (GEMG). Publisher adoption of schema.org/JSON-LD in
  news went global years ago. (primary source; verification rate-limited)
- JSON-LD `NewsArticle.articleBody` is the publisher's *own canonical* article text: zero
  boilerplate, plus headline/date/author/language/canonical — when present, it beats any
  heuristic extractor by construction.
- **Live blogs have a dedicated schema: `LiveBlogPosting` with per-update `liveBlogUpdate`
  entries** (each its own headline/body/timestamp). Parsing it turns our worst input — a 30k
  noisy scroll — into a clean, timestamped sequence of update bodies. BBC and Le Monde both
  emit it.
- Caveats from GDELT's own experience: `@type` is inconsistent (same content as NewsArticle /
  Article / WebPage / even Product), pages embed *multiple* JSON-LD blocks that must all be
  parsed and reconciled, useful fields nest below the root, and richness varies wildly — so
  JSON-LD is a *layer*, never the whole answer.

### 1.6 Claims the verification panel killed (do not rely on these)

- "rs-trafilatura tops the WCXB benchmark at F1 0.859 + 44ms" — refuted 0-3.
- "Commercial ML services topped the 2019 benchmark at 0.970/0.951" — refuted 0-3 (numbers
  misread from the table).
- "Main-content extraction is effectively solved (all systems ≥0.871 on articles)" — refuted
  1-2 (true only for the article genre, and precisely not for our failure cases).

Benchmarks are predominantly English/German; multilingual performance must be validated on our
own traffic. The ScrapingHub benchmark repo is archived (last push 2026-05).

---

## 2. Recommended architecture for Maat

One new module — `acquire/content.py` (name TBD) — behind the existing `FetchedPage` seam, so
`fetch_page()` callers (analyse, GDELT leg, live candidates) upgrade without signature changes.
A **ladder**: deterministic and cheap first, expensive and hostile-capable last, every rung
instrumented.

```
URL
 │
 ├─ L0  identity/cache (exists: canonical dedup, analysis cache)
 │
 ├─ L1  FETCH, polite-but-not-naive
 │      curl-cffi Chrome-impersonated GET (2-4s timeout)
 │      → on 403/challenge/empty: mark "hostile", skip to L4
 │
 ├─ L2  STRUCTURED DATA (parse ALL JSON-LD blocks from the HTML we already have)
 │      NewsArticle/ReportageNewsArticle.articleBody present & substantial → DONE (best case)
 │      LiveBlogPosting → segment into liveBlogUpdate items → DONE (live-blog fix)
 │      always harvest metadata (headline/date/author/canonical) even when body is thin
 │
 ├─ L3  GENERIC EXTRACTION (same HTML, <100ms)
 │      per-site learned schema, if one is cached for this domain → run it (deterministic)
 │      else trafilatura + readability-lxml, agreement-checked
 │        (large disagreement or thin result → try Fundus parser if publisher supported;
 │         flag domain for schema learning)
 │
 ├─ L4  HOSTILE-FETCH ESCALATION (only when L1 failed or L3 yielded < min_chars)
 │      managed unblocker call (recommendation: Zyte API, extractFrom=browserHtml —
 │        one request = stealth fetch + rendered HTML + their article extraction as a
 │        cross-check); alternative: self-hosted Camoufox + residential proxy
 │      then archive fallback for hard paywalls: archive.today / archive.org snapshot
 │
 └─ L5  SCHEMA LEARNING (async, off the request path)
        domains that recur and fail/underperform L3 → LLM generates a CSS/XPath schema from
        2-3 sample pages (crawl4ai pattern) → validated deterministically against live HTML →
        stored (events log) → self-healing: on schema failure, re-learn once, then escalate
        to operator (console surface). LLM proposes; deterministic code decides.
```

Cross-cutting, non-optional:

- **Telemetry**: every fetch records which rung served it, body length, duration, and failure
  reasons — the Le Pen bug was invisible because every failure path was `except: pass`. The
  ladder must be observable in the console.
- **Golden regression set**: snapshot HTML + expected body for the top ~30 publishers we
  actually see (incl. one live blog each from BBC/Le Monde/Guardian); run in CI like the
  benchmarks above run offline. Extractor and schema changes get measured, not vibed.
- **Candidate parity**: live-corroboration candidates stop trusting Apify's `text` blindly —
  Apify remains the *search* provider, but bodies flow through this same ladder (Apify text
  kept as one more input, not the only one). Empty Reuters/NYT bodies then escalate to L4
  instead of silently dropping the corroborator.
- **Latency budget** (paste-a-URL path): L1 1–3s; L2+L3 <0.2s; L4 5–20s but rare and parallel
  per candidate; claim extraction unchanged. Analysis wall-clock should *improve* (cleaner,
  shorter input to the LLM).

### Adopt vs build

| Adopt (exists, benchmarked) | Build (ours) |
|---|---|
| trafilatura (keep, primary) + readability-lxml (agreement check) | the ladder/orchestrator + rung telemetry |
| curl-cffi (impersonated fetch) | JSON-LD block parsing/reconciliation + LiveBlogPosting segmentation |
| extruct or lxml for JSON-LD | per-site schema store + validation + self-healing/escalation |
| Fundus (per-publisher parsers where covered) | golden-set regression harness |
| Zyte API (or Scrapfly) for the hostile tier | candidate-parity wiring in serving/analyse |
| crawl4ai `generate_schema` (pattern; can implement natively) | |

### Decision points for cauri

1. **Hostile tier: managed (Zyte API / Scrapfly) vs self-hosted (Camoufox + residential
   proxies)?** Recommendation: managed (Zyte API) — the anti-bot arms race is exactly the kind
   of undifferentiated maintenance we shouldn't own, pay-per-success, and it doubles as an
   extraction cross-check. Self-hosted remains the sovereignty fallback.
2. **Archive fallback (archive.today/org) for hard paywalls: in or out?** It works (it's what
   readers and many aggregators use) but is the greyest zone legally/ToS-wise of anything here.
   Everything else in the ladder reads publicly served pages.

## 3. Sources (verified findings)

- github.com/scrapinghub/article-extraction-benchmark — canonical extractor benchmark
- dl.acm.org/doi/10.1145/3539618.3591920 — Bevendorff et al., SIGIR 2023 reproduction study
- osti.gov/servlets/purl/2429881 — independent 7-library evaluation
- arxiv.org/html/2605.21097 — WCXB benchmark incl. neural extractors' latency/accuracy
- researchgate.net/publication/353488798 — trafilatura paper (architecture, DANIEL multilingual)
- arxiv.org/html/2403.15279 — Fundus (per-publisher parsers)
- arxiv.org/html/2605.18337 — Infini-News (trafilatura at 1.36B-article scale)
- docs.diffbot.com/reference/extract-introduction · docs.zyte.com/zyte-api/usage/extract ·
  zyte.com/zyte-api/ai-extraction — commercial extraction architectures
- blog.gdeltproject.org (JSON-LD scanning; GEMG) — aggregator structured-data practice
- docs.crawl4ai.com/extraction/no-llm-strategies — generate-schema-once pattern
- scrapfly.io blog (anti-bot layers, Cloudflare bypass) · blog.apify.com/jina-ai-vs-firecrawl —
  hostile-fetch engineering (vendor blogs; treated as practitioner testimony, not benchmarks)
- dev.to/viniciuspuerto — self-healing selector-repair system (practitioner report)
