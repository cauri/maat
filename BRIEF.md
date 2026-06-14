# Build Spec — Personal News Feed with Veracity Weighting

> The original product brief, verbatim — the source of truth for product *why*.
> Engineering decisions and sequencing live in `PLAN.md` and `DECISIONS.md`.

A personal, continuous news feed that ranges across the open web, assesses how well each story corroborates rather than how widely it spreads, and serves it with a confidence read attached. It weights away from a US-centric slant, spans English, Portuguese, and French sources, and reads natively on iPhone and Mac. It serves more than one user from the start. Quality takes priority over cost throughout.

## 1. Product
The feed accumulates and develops stories continuously, closer to Apple News in feel than to a once-daily brief. It stays warm through the day; the reader scrolls rather than opening a digest.

No fixed source list governs it. The system searches the open web, judges veracity from corroboration, gates only the genuinely untrustworthy, and labels the rest with confidence. It blends general world news with the reader's own topics, given in natural language. Seed topic: world politics and AI news.

The reader can comment on a story (freeform text) and pin a story to follow it.

The why: the goal centres on escaping both the region-lock and the American centre-of-gravity of existing apps, and on trusting corroborated truth over loud consensus. Every decision below serves that goal.

## 2. Architecture
At its core this runs as a multi-agent system. Independent agents coordinate with each other over A2A (agent-to-agent), and each agent reaches its tools, data, and context over MCP (Model Context Protocol). Two views follow: the agent topology (what the components are and how they talk) and the compute tiers (where the work runs).

This architecture reflects what we know so far. Expect it to change as the build teaches us more.

### Agents and protocols
Agents coordinate over A2A; each agent reaches its tools, sources, the canonical store, and on-device capabilities over MCP. Named agents so far: the acquisition agent and the curation agent (§3). The pipeline stages listed in Tier 1 below (extraction, classification, clustering, corroboration scoring, threading, reputation, accuracy, harvesting) may resolve into further agents or run as tools the agents call over MCP — that decomposition remains unsettled.

### Compute tiers

```
   ┌─────────────────────────────────────────────────────────┐
   │  TIER 1 — SERVER  (frontier model: Claude API)          │
   │  Runs while devices sleep. Carries every hard problem.  │
   │  · news-acquisition agent (broad search -> feeds)       │
   │  · curation agent (what reaches the reader, ordering)   │
   │  · claim extraction + attribution/assertion classify    │
   │  · fact/projection classify                             │
   │  · claim-clustering (corroboration)                     │
   │  · corroboration scoring (independent originators)      │
   │  · veracity gate + confidence labels                    │
   │  · event-node graph (threading)                         │
   │  · reputation (truthfulness) + accuracy (forecasts)     │
   │  · projection-harvester clock                           │
   │  · canonical store                                      │
   └──────────────────────────┬──────────────────────────────┘
                              │ serves feed + claims + labels
   ┌──────────────────────────┴──────────────────────────────┐
   │  TIER 2 — ON-DEVICE  (Apple Foundation Models, Swift)   │
   │  iPhone + Mac. Free, private, instant.                  │
   │  · re-rank served feed against natural-language topics  │
   │  · re-summarise each story to the reader's taste        │
   │  · on-device semantic search across stored stories      │
   │  · hold comments locally                                │
   │  · (later) on-device LoRA personalisation adapters      │
   └──────────────────────────┬──────────────────────────────┘
                              │ one-line escalation when needed
   ┌──────────────────────────┴──────────────────────────────┐
   │  TIER 3 — PRIVATE CLOUD COMPUTE  (Apple 32K reasoning)  │
   │  Middle tier / fallback / "go deeper on this story".    │
   └─────────────────────────────────────────────────────────┘
```

The veracity core runs server-side on a frontier model rather than on Apple's Private Cloud Compute. Apple's own guidance rules its on-device model out of factual assessment and world knowledge. PCC offers a unified Swift API, 32K context, and reasoning, but the corroboration reasoning that defines the product needs the strongest available model, and quality takes priority over cost.

The client counts as a real compute tier, not a thin shell. The Apple Foundation Models framework gives the device model guided generation (type-safe Swift structs via `@Generable`), tool calling, streaming, and built-in semantic search, on both iPhone and Mac, at zero marginal cost and offline. Re-ranking, summarise-to-taste, and local search belong on-device for that reason.

Target hardware: a current iPhone and Mac, both Apple-Intelligence-capable, so the on-device tier runs natively on both ends.

## 3. Agents
Two agents own distinct jobs. They coordinate over A2A and reach their tools, data, and context over MCP.

The acquisition agent runs as a learning loop, not a fixed method. It starts broad with web search, then learns over time which sources reward attention and zeroes in on feeds, growing smarter about where good news comes from with experience. It narrows from open search toward trusted feeds as evidence accumulates. The source-reputation store (§6) and the agent's "where to look" learning draw on the same source-level signal, so the two reinforce each other.

The curation agent decides what reaches the reader and how it orders. It runs across the served ranking (server) and the on-device re-rank (Tier 2).

The split: acquisition decides where to look and what to pull; curation decides what reaches the reader and in what order.

## 4. Multi-tenancy
The system serves more than one user from the start, not as a later retrofit.

Shared substrate, one truth layer across all users, growing better as more users feed it: the veracity engine, reputation, accuracy, the event-node graph, and source-learning.

Per-user: topics, comments, pins, on-device personalisation, and the feed view.

The why: reputation must stay shared, or scale stops strengthening the corroboration signal that more users are meant to improve. Personalisation must stay per-user, or one reader's taste leaks into another's feed.

## 5. The veracity engine
Each incoming article passes through this pipeline. The claim, not the article, forms the unit of everything downstream.

### 5.1 Extract claims
Pull the atomic assertions out of the article.

### 5.2 Classify each claim: attribution vs assertion
Most articles hold two layers:

* Reported object — "X said Y." Truth condition: did X say Y? If yes, the outlet reported a truth, even when Y itself lies.
* Embedded claim — Y itself, scored as its own claim. Its falsehood attaches to the speaker X, never to the outlet that faithfully quoted X.

So an outlet faithfully quoting a liar earns a correct mark; the lie dings the speaker.

The protection holds only for genuine attribution. The classifier must catch three abuses and, in each, make the outlet own the claim:

1. Endorsement — "Y, as X rightly said." The outlet adopted the claim.
2. Bare repetition as fact — the outlet states Y in its own voice in the headline, then buries "X said" deep in the body. The headline asserted; it owns the headline.
3. Selective amplification — always-faithful quoting, but only ever of one side's falsehoods. Each article scores clean while the aggregate reveals slant. This one shows only at the source level over time, not per article.

The why: a publication accurately reporting a real utterance reports a truth, regardless of the utterance's content. Without this split, the system would punish honest reporting of dishonest people, and the laundering guards stop bad actors from hiding assertions inside fake attribution.

### 5.3 Within assertion, classify: fact vs projection

* Fact — a present-tense claim about the world, with a truth value now. Routes to reputation (§6).
* Projection — a forecast or judgement about an unresolved future, with no truth value yet. Routes to accuracy (§7). Never to reputation.

This tag guards the wall between the two scoring axes. The grey zone to catch: "the deal will collapse" (projection) versus "the deal is collapsing" (present-tense fact wearing a trajectory). A misclassification leaks a forecast into truthfulness scoring.

Conclusions an article draws in its own voice split the same way:

* Synthesis — derives a new factual claim from on-record claims ("these three contracts share a shell company, therefore coordination"). Enters as a normal assertion originated by the outlet. Its corroboration runs against whether the synthesis holds up, not against whether each input fact held true. An outlet can reason from true premises to a false conclusion, and that conclusion dinging its reputation reads as correct.
* Analysis, opinion, or forecast — a projection. Accuracy axis.

### 5.4 Cluster claims across the story
Group claims that assert the same fact, using tight identity (near-synonymous assertions only). This differs sharply from threading (§8); the two clustering jobs stay separate.

### 5.5 Score corroboration by independent originators, not spread
Coverage volume counts for almost nothing. Three things collapse a hundred outlets to a single originator:

* Wire syndication — AFP, Reuters, AP write once; hundreds reprint near-verbatim.
* Citation cascades — everyone writes "according to [the original report]." One originator; the rest point at it.
* Common ownership and copy-networks — outlets under one roof, or known to lift from each other, share a node.

Count independent originators of a claim. Weight primary sources — a named official, a filed document, a dataset, on-the-ground reporting — above any amount of secondary repetition. Thirty outlets echoing one unverified figure score as one thin thread, not thirty.

The why: spread rewards whatever travels fastest, which sensational falsehood does better than careful truth. Independent corroboration measures the thing that actually bears on whether a claim holds.

### 5.6 Scale the bar to the claim's extremity
Confidence uses no fixed threshold. Each claim carries a prior; the corroboration weight needed to clear it rises with the claim's distance from that prior. "The minister resigned" clears on modest corroboration; "the minister assassinated a rival" demands far more, and from stronger origins. Extraordinary claims, extraordinary evidence. This also hardens the corroboration defence, since the loudest viral claims tend to sit furthest from prior.

### 5.7 Gate, then label

* Gate only the floor: suppress claims below a genuinely low veracity threshold.
* Label everything above with a confidence read.

Do not gate above the floor. Gating above the floor hands editorial power to a model that will misjudge, which recreates the curated feed the product exists to escape.

Story-level confidence rolls up from its claim confidences.

## 6. Entities and reputation

### 6.1 Every entity carries a reputation
Reputation lives on any node that can author or carry a claim:

* Outlets — scored on faithful attribution and on claims asserted in their own voice.
* Speakers — people and named officials, scored on originated claims proving out.
* Institutions — companies, ministries, agencies issuing statements, distinct from the individuals fronting them.
* Documents and datasets — primary sources; highest default standing.

One claim threads through a chain of entities (speaker uttered it, outlet carried it, document grounds it); each link scores on its own footing.

### 6.2 Reputation means truthfulness — one scalar, domain-independent
Reputation answers one question: does this entity tell the truth. A single scalar per entity. Honesty does not partition by subject, so reputation does not key by domain.

Reputation differs from expertise (knowing a subject). Expertise would key by domain, but the model does not track expertise at all (§10). An entity offering a truthful opinion outside its competence stays truthful; reputation stays intact.

This resolves the habitual-liar case with no sub-scores: a liar holds a low reputation, one number, while "X tweeted Y" stays reliable through attribution alone (§5.2), because the outlet truthfully reports that the tweet exists.

### 6.3 Judge against eventual primary truth, never consensus
Reputation moves on originated factual claims that later prove out or fall apart, judged against documents, retractions, or strong independent corroboration. It never moves on agreement with the consensus of the moment. Scoring sources on matching the crowd builds a conformity machine that punishes the outlet which breaks a true story early and rewards the herd. Echoing earns little reputation either way.

### 6.4 Reputation as a time-trajectory
Store reputation as a value with history, not only a latest number, because forward-resolving projections must credit or debit the entity as it stood at resolution, and because a reader may want to know whether an outlet held a good reputation at the time it ran a given story.

Decay toward the recent. Treat regime breaks — ownership change, an editor leaving — as partial resets, not a blind average across them.

### 6.5 Backfill as a decaying, capped prior
Bootstrap reputation by replaying historical news across any chosen window. History forms the one regime where the eventual-primary-truth signal already exists: resolved claims arrive with the answer key in hand, so this scores against the same anti-consensus anchor, legitimately.

Three constraints, all required:

1. Decay and regime resets — a flat multi-year score misweights an outlet hollowed out late in the window.
2. Resolved subset only — many old claims never resolved into documented truth; score only those that did, and stay agnostic on the rest, or the conformity machine slips back in.
3. Archive-bias correction — archives over-represent large English-language majors; the long tail, local, Portuguese, and French sources sit thin or absent. A naive backfill therefore amplifies the exact US and Anglo slant the product exists to escape. Backfill acts as a decaying prior that live evidence overwrites, capped so history alone cannot permanently anchor the majors.

### 6.6 Cold-start: unknown does not mean untrustworthy
Most entities arrive unknown. Reason from a neutral prior and let reputation accrue. Never read "unknown" as "untrustworthy" — that repeats the conformity failure, since the outlet that breaks a true story first will often be one the system has barely seen.

### 6.7 Identity resolution
Match the same speaker across outlets, separate spokesman from institution, track outlet rebrands. Wrong entity-matching smears reputation across the wrong nodes, so this step carries real weight.

## 7. Accuracy — a separate axis
Accuracy means forecasting track record. It moves only on resolved projections and never touches reputation.

A wrong forecast made in good faith does not constitute dishonesty; it constitutes a miss. Collapsing the two would bleed reputation from any outlet bold enough to make falsifiable calls while leaving the cowardly forecaster pristine, which inverts the goal.

Lag mechanism: a projection enters dormant — tagged as a projection, attached to its spines, carrying a resolution horizon, held out of present confidence so it contributes nothing to the live story label. When the horizon arrives and the world resolves it, it pays into accuracy.

Resolve, extend, or decay: most projections will not resolve cleanly on the dot. Resolve when evidence genuinely suffices; extend when not; let a never-resolving projection decay to negligible weight rather than forcing a verdict. Hanging forever costs nothing, as long as only resolved projections move the ledger.

Accuracy computes from day one, since the ledger must accrue to hold any value when wanted, but it does not display yet (§10).

## 8. Story structure — a graph

### 8.1 Two clustering jobs, opposite behaviours

* Claim-clustering (§5.4) — "same fact?" — tight identity. Inside a development.
* Story-threading — "same developing event?" — loose continuity. A persistent spine that new articles attach to even after their specific claims have entirely turned over (quake -> rescue -> inquiry -> verdict).

A thread therefore does not mean "articles sharing claims." It means a persistent event identity.

### 8.2 Threads form a graph of event-nodes
Model threads as a graph, not flat buckets. Event-nodes connect by typed edges: develops, spawns, merges. This represents the cases flat containers cannot — a fire that spawns a safety inquiry, a war that splits across fronts, a fraud and a donations scandal that merge on a shared culprit. These reflect how real stories move.

### 8.3 Membership lives at the claim, not the article
A story does not form a single thing. One article's claims point at different spines, so one article belongs to multiple threads by default. Two linked layers:

* Claims (atomic) — carry their entities, confidence, the fact/projection tag and optional horizon, and a many-to-many attachment to one or more event-nodes.
* Event-nodes (spines) — persistent event identities; gather the claims that belong to a development regardless of which article delivered them; connect by typed edges.
* Articles — provenance envelopes (byline, timestamp, the attribution layer). They own nothing structurally, but can originate claims when they synthesise or conclude in their own voice (§5.3).

The claim-to-node attachment carries quality weight, like attribution classification: misplacement either fractures a spine or pollutes one. The conversation settled the structure — claims attach to one or more spines, many-to-many — but did not settle the attachment mechanism (whether a claim reaches a spine by its content alone or also by the entities it names, and consequently which entities deserve spines of their own).

### 8.4 Dedup, "you've seen this", on claim-novelty
An article surfaces when it carries claims not yet seen on its node, or attaches a new node to a spine the reader follows. This catches the wire-reprint case for free — identical claims across twenty envelopes, one already seen, the other nineteen suppressed with no special rule. The delta to surface reads as "the thing you follow moved" — not the article already read, not a silent suppression.

## 9. The two clocks

1. Acquisition and ingestion cycle — every few hours: the acquisition agent pulls wide; the engine extracts, classifies, clusters, scores, threads, labels, stores. It tracks running stories and surfaces what changed since the last pull, rather than re-deriving the world each cycle.
2. Projection harvester — wakes on schedule to check matured projections against their horizons and resolve, extend, or decay them into the accuracy ledger.

## 10. Left out or deferred

* Expertise / domain competence — left out entirely. No domain-keyed scoring, no per-domain cold-start, no standing-versus-honesty bookkeeping. Reputation tracks truthfulness alone, and truthfulness does not partition by subject.
* Storage schema / data model — not designed ahead of real data. Capture raw articles, claims, entities, scores, and graph relations in the simplest durable store that works, and let the real data shape the model later.
* Comment feedback machinery — gather comments only for now: capture text, which story, and timestamp. Separately, record which sources expose reader-comment mechanisms to their own readers, against a later possibility of reposting the reader's comments upstream. Build abilities on top once real comments exist.
* Push notifications — added once the feed proves itself.
* Accuracy display — computed quietly now (§7); surfaced later, since showing a forecast track record well forms its own design problem.
