"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { Analysis, PublicClaim } from "@/lib/types";
import ShareCard from "./share-card";

// Wire types (serving/analyse.py — verdicts only, never the mechanism) live in @/lib/types so the
// page's OG metadata and the card renderer share one definition.

type Skeleton = { text: string; speaker: string | null; central: boolean };
type ChipState = "queued" | "checking";
type Phase = "idle" | "running" | "done" | "error";

const EXTREMITY_LEVELS = ["routine", "ordinary", "notable", "significant", "extraordinary"];

// ── SSE over fetch (POST) ───────────────────────────────────────────────────────────────────────

async function* sseEvents(res: Response): AsyncGenerator<{ event: string; data: unknown }> {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      if (!block.trim() || block.startsWith(":")) continue; // keep-alive comment
      let event = "message";
      const dataLines: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7).trim();
        else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
      }
      if (dataLines.length) {
        try {
          yield { event, data: JSON.parse(dataLines.join("\n")) };
        } catch {
          /* malformed frame — skip */
        }
      }
    }
  }
}

// ── presentation ────────────────────────────────────────────────────────────────────────────────

function ExtremityMini({ level }: { level: string }) {
  const n = EXTREMITY_LEVELS.indexOf(level) + 1;
  return (
    <span
      className="extremity"
      title={`How surprising this claim is: ${level}. The more extraordinary a claim, the more independent confirmation Maat requires before it counts as solid.`}
    >
      <span className="dots" aria-hidden>
        {EXTREMITY_LEVELS.map((_, i) => (
          <i key={i} className={i < n ? "on" : ""} />
        ))}
      </span>
      {level}
    </span>
  );
}

function ScoreBlock({ claim, state }: { claim?: PublicClaim; state: ChipState }) {
  if (!claim) {
    return (
      <div className="score-col pending" aria-hidden>
        <span className={`weigh-glyph ${state === "checking" ? "busy" : ""}`}>⚖</span>
      </div>
    );
  }
  const smelly = claim.tier === "floor" || claim.verdict.startsWith("Disputed");
  return (
    <div className={`score-col tier-${claim.tier}`}>
      <span className="big-score">{claim.score}</span>
      <span className="denom">/100</span>
      {smelly && (
        <span className="stink" role="img" aria-label="reads well below the bar" title="reads well below the bar">
          💩
        </span>
      )}
    </div>
  );
}

function ClaimRow({ claim, skeleton, state }: { claim?: PublicClaim; skeleton: Skeleton; state: ChipState }) {
  return (
    <div className={`claim ${claim ? "resolved" : "unresolved"}`}>
      <ScoreBlock claim={claim} state={state} />
      <div className="main-col">
        <p className="text">
          {skeleton.text}
          {skeleton.central && <span className="central-tag">central claim</span>}
        </p>
        {skeleton.speaker && <span className="speaker">— attributed to {skeleton.speaker}</span>}
        {claim ? (
          <span className={`verdict tier-${claim.tier}`}>{claim.verdict}</span>
        ) : (
          <span className="weighing">
            {state === "checking" ? "checking against independent reporting…" : "queued for weighing…"}
            <span className="shimmer" aria-hidden />
          </span>
        )}
      </div>
      <div className="side-col">{claim && <ExtremityMini level={claim.extremity} />}</div>
    </div>
  );
}

// ── the page ────────────────────────────────────────────────────────────────────────────────────

export default function Analyser() {
  const [url, setUrl] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [activity, setActivity] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [meta, setMeta] = useState<{ title: string | null; source: string; date: string | null } | null>(null);
  const [skeletons, setSkeletons] = useState<Skeleton[]>([]);
  const [claims, setClaims] = useState<(PublicClaim | undefined)[]>([]);
  const [chipStates, setChipStates] = useState<ChipState[]>([]);
  const [dropped, setDropped] = useState(0);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const running = useRef(false);

  // A shared link (?id=an-…) renders the stored analysis without re-running it.
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("id");
    if (!id) return;
    fetch(`/api/analyse/${encodeURIComponent(id)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d?.analysis) {
          setAnalysis(d.analysis as Analysis);
          setUrl((d.analysis as Analysis).url);
          setPhase("done");
        }
      })
      .catch(() => {});
  }, []);

  const analyse = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (running.current || !url.trim()) return;
      running.current = true;
      setPhase("running");
      setError(null);
      setAnalysis(null);
      setMeta(null);
      setSkeletons([]);
      setClaims([]);
      setChipStates([]);
      setDropped(0);
      setActivity("Fetching the article…");
      try {
        const res = await fetch("/api/analyse", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: url.trim() }),
        });
        if (res.status === 429) {
          throw new Error("You've hit the analysis limit for now — try again in a few minutes.");
        }
        if (!res.ok || !res.body) {
          throw new Error("Something went wrong starting the analysis — try again shortly.");
        }
        for await (const { event, data } of sseEvents(res)) {
          const d = data as Record<string, unknown>;
          if (event === "fetched") {
            setMeta({
              title: (d.title as string) ?? null,
              source: (d.source as string) ?? "",
              date: (d.date as string) ?? null,
            });
            setActivity("Reading the article and pulling out every claim it makes…");
          } else if (event === "extracted") {
            const facts = (d.facts as Skeleton[]) ?? [];
            setSkeletons(facts);
            setClaims(new Array(facts.length).fill(undefined));
            setChipStates(new Array(facts.length).fill("queued"));
            setDropped((d.dropped as number) ?? 0);
            setActivity(
              `${facts.length} factual claim${facts.length === 1 ? "" : "s"} found — weighing each one…`,
            );
          } else if (event === "matched") {
            const m = (d.matched as number) ?? 0;
            if (m > 0) setActivity(`${m} claim${m === 1 ? "" : "s"} Maat already knows — checking the rest…`);
          } else if (event === "searching") {
            const n = (d.claims as number) ?? 0;
            setActivity(`Searching independent reporting for ${n} claim${n === 1 ? "" : "s"} — this can take a minute…`);
          } else if (event === "checking") {
            const idx = d.index as number;
            setChipStates((prev) => {
              const next = prev.slice();
              next[idx] = "checking";
              return next;
            });
          } else if (event === "claim") {
            const idx = d.index as number;
            const claim = d.claim as PublicClaim;
            setClaims((prev) => {
              const next = prev.slice();
              next[idx] = claim;
              return next;
            });
          } else if (event === "done") {
            const a = (d as { analysis: Analysis }).analysis;
            setAnalysis(a);
            setPhase("done");
            setActivity("");
            window.history.replaceState(null, "", `?id=${encodeURIComponent(a.analysis_id)}`);
          } else if (event === "error") {
            throw new Error((d.detail as string) || "Analysis failed — try again shortly.");
          }
        }
        setPhase((p) => (p === "running" ? "error" : p));
      } catch (err) {
        setPhase("error");
        setActivity("");
        setError(err instanceof Error ? err.message : "Analysis failed — try again shortly.");
      } finally {
        running.current = false;
      }
    },
    [url],
  );

  const shown = analysis
    ? {
        skeletons: analysis.claims.map((c) => ({ text: c.text, speaker: c.speaker, central: c.central })),
        claims: analysis.claims as (PublicClaim | undefined)[],
      }
    : { skeletons, claims };

  const publisher = analysis?.publisher;

  return (
    <>
      <form className="paste" onSubmit={analyse}>
        <input
          type="url"
          required
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https:// — paste an article link"
          aria-label="Article URL"
          disabled={phase === "running"}
        />
        <button type="submit" disabled={phase === "running"}>
          {phase === "running" ? "Analysing…" : "Analyse"}
        </button>
      </form>

      {phase === "running" && (
        <div className="activity" role="status">
          <span className="feather-anim" aria-hidden>
            ⚖
          </span>
          <span className="activity-text" key={activity}>
            {activity}
          </span>
        </div>
      )}
      {error && <div className="error">{error}</div>}

      {(meta || analysis) && (
        <div className="card article-head">
          <h2>{analysis?.title ?? meta?.title ?? "Untitled article"}</h2>
          <div className="meta">
            {(analysis?.source ?? meta?.source) || ""}
            {(analysis?.date ?? meta?.date) ? ` · ${analysis?.date ?? meta?.date}` : ""}
          </div>
          {publisher && (
            <div className={`pub-badge ${publisher.rated ? "rated" : ""}`}>
              {publisher.rated ? (
                <>
                  Publisher track record: <strong>{publisher.score}/100</strong>
                </>
              ) : publisher.review_started ? (
                <>No track record yet — Maat has started reviewing this publisher&rsquo;s past reporting.</>
              ) : (
                <>Publisher not yet rated.</>
              )}
            </div>
          )}
        </div>
      )}

      {analysis && (
        <div className="card overall" aria-live="polite">
          <div className="band-row">
            <span className={`band band-${analysis.overall.band}`}>{analysis.overall.label}</span>
            {!analysis.overall.forecast_only && <span className="num">· {analysis.overall.score}/100</span>}
          </div>
          {analysis.overall.reasons.length > 0 && (
            <ul className="reasons">
              {analysis.overall.reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          )}
          <div className="scope">{analysis.scope}</div>
        </div>
      )}

      {shown.skeletons.length > 0 && (
        <section aria-live="polite">
          <h3 className="section">The claims</h3>
          {dropped > 0 && (
            <p className="dropped-note">
              {dropped} snippet{dropped === 1 ? "" : "s"} discarded — could not be verified against the page text.
            </p>
          )}
          <div className="card">
            {shown.skeletons.map((s, i) => (
              <ClaimRow key={i} skeleton={s} claim={shown.claims[i]} state={chipStates[i] ?? "queued"} />
            ))}
          </div>
        </section>
      )}

      {analysis && analysis.projections.length > 0 && (
        <section className="projections">
          <h3 className="section">Forecasts &amp; opinions — not scored for truth</h3>
          <div className="card">
            {analysis.projections.map((p, i) => (
              <div className="claim resolved" key={i}>
                <div className="score-col tier-none">
                  <span className="big-score muted">—</span>
                </div>
                <div className="main-col">
                  <p className="text">{p.text}</p>
                  {p.speaker && <span className="speaker">— attributed to {p.speaker}</span>}
                  <span className="verdict tier-none">{p.verdict}</span>
                </div>
                <div className="side-col" />
              </div>
            ))}
          </div>
        </section>
      )}

      {analysis && <ShareCard analysis={analysis} />}
    </>
  );
}
