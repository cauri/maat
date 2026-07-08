"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Analysis, PublicClaim } from "@/lib/types";
import MiniPage, { type MiniFact } from "./mini-page";
import ShareCard from "./share-card";
import Weighing from "./weighing";

// Wire types (serving/analyse.py — verdicts only, never the mechanism) live in @/lib/types so the
// page's OG metadata and the card renderer share one definition.

type Skeleton = { text: string; speaker: string | null; central: boolean; position: number };
type ChipState = "queued" | "checking";
type Phase = "idle" | "reading" | "weighing" | "done" | "error";

const EXTREMITY_LEVELS = ["routine", "ordinary", "notable", "significant", "extraordinary"];

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
      if (!block.trim() || block.startsWith(":")) continue;
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

// ── significance marker — small + quiet by design (shown, never competing with the score) ──
function Significance({ level }: { level: string }) {
  const n = EXTREMITY_LEVELS.indexOf(level) + 1;
  return (
    <span
      className="sig"
      title={`How surprising this claim is: ${level}. The more extraordinary, the more independent confirmation Maat requires.`}
    >
      <span className="dots" aria-hidden="true">
        {EXTREMITY_LEVELS.map((_, i) => (
          <i key={i} className={i < n ? "on" : ""} />
        ))}
      </span>
      <span className="sig-label">{level}</span>
    </span>
  );
}

function ClaimRow({
  skeleton,
  claim,
  state,
  rowRef,
}: {
  skeleton: Skeleton;
  claim?: PublicClaim;
  state: ChipState;
  rowRef: (el: HTMLElement | null) => void;
}) {
  const smelly = claim && (claim.tier === "floor" || claim.verdict.startsWith("Disputed"));
  return (
    <div ref={rowRef} className={`claim ${claim ? "resolved" : "unresolved"}`}>
      {claim ? (
        <div className="score-col">
          <span className={`big-score tier-${claim.tier}`}>{claim.score}</span>
          <span className="denom">/100</span>
          {smelly && (
            <span className="stink" role="img" aria-label="reads well below the bar">
              💩
            </span>
          )}
        </div>
      ) : (
        <div className="score-col pending" aria-hidden="true">
          <span className={`weigh-glyph ${state === "checking" ? "busy" : ""}`}>⚖</span>
        </div>
      )}
      <div className="main-col">
        <p className="text">
          {skeleton.text}
          {skeleton.central && <span className="central-tag">central claim</span>}
        </p>
        {skeleton.speaker && <span className="speaker">— attributed to {skeleton.speaker}</span>}
        {claim ? (
          <>
            <span className={`verdict tier-${claim.tier}`}>{claim.verdict}</span>
            <Significance level={claim.extremity} />
          </>
        ) : (
          <span className="weighing">
            {state === "checking" ? "checking against independent reporting…" : "queued for weighing…"}
            <span className="shimmer" aria-hidden="true" />
          </span>
        )}
      </div>
    </div>
  );
}

export default function Analyser() {
  const [url, setUrl] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [activity, setActivity] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [meta, setMeta] = useState<{ title: string | null; source: string; date: string | null } | null>(null);
  const [skeletons, setSkeletons] = useState<Skeleton[]>([]);
  const [claims, setClaims] = useState<(PublicClaim | undefined)[]>([]);
  const [chipStates, setChipStates] = useState<ChipState[]>([]);
  const [resolvedCount, setResolvedCount] = useState(0);
  const [dropped, setDropped] = useState(0);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [morphComplete, setMorphComplete] = useState(false);
  const running = useRef(false);
  const morphed = useRef(false);
  const highlightRefs = useRef<Map<number, HTMLElement>>(new Map());
  const rowRefs = useRef<Map<number, HTMLElement>>(new Map());
  const stageRef = useRef<HTMLDivElement>(null);

  // A shared link (?id=an-…) renders the stored analysis directly — no reading/weighing.
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

  // Once the article is read (claims found + highlighted), tip into weighing.
  useEffect(() => {
    if (phase !== "reading" || skeletons.length === 0) return;
    const t = setTimeout(() => setPhase("weighing"), 1200);
    return () => clearTimeout(t);
  }, [phase, skeletons.length]);

  // The morph: each highlighted claim flies from its spot in the mini-page into its list row.
  // All work + state updates run inside a rAF callback (measure after layout; also keeps setState
  // out of the effect body). Reduced-motion or any measurement gap → clean reveal (never breaks).
  useLayoutEffect(() => {
    if (phase !== "weighing" || morphed.current) return;
    morphed.current = true;
    const stage = stageRef.current;
    let timer = 0;
    const raf = requestAnimationFrame(() => {
      const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      const hs = highlightRefs.current;
      const rs = rowRefs.current;
      if (reduce || !stage || hs.size === 0 || rs.size === 0) {
        setMorphComplete(true);
        return;
      }
      let base: DOMRect;
      try {
        base = stage.getBoundingClientRect();
      } catch {
        setMorphComplete(true);
        return;
      }
      let pending = 0;
      let animated = false;
      hs.forEach((hEl, i) => {
        try {
          const rEl = rs.get(i);
          if (!rEl) return;
          const h = hEl.getBoundingClientRect();
          const r = rEl.getBoundingClientRect();
          if (h.width === 0 || r.width === 0) return;
          const chip = document.createElement("div");
          chip.className = "fly-chip";
          chip.style.left = `${h.left - base.left}px`;
          chip.style.top = `${h.top - base.top}px`;
          chip.style.width = `${h.width}px`;
          chip.style.height = `${h.height}px`;
          stage.appendChild(chip);
          pending++;
          animated = true;
          const dx = r.left - base.left - (h.left - base.left);
          const dy = r.top - base.top - (h.top - base.top);
          const anim = chip.animate(
            [
              { transform: "translate(0,0)", opacity: 0.95 },
              { transform: `translate(${dx}px, ${dy}px)`, opacity: 0 },
            ],
            { duration: 640, delay: i * 65, easing: "cubic-bezier(.5,0,.2,1)", fill: "forwards" },
          );
          anim.onfinish = () => {
            chip.remove();
            if (--pending === 0) setMorphComplete(true);
          };
        } catch {
          /* skip this chip — rows still reveal via the safety timer */
        }
      });
      if (!animated) {
        setMorphComplete(true);
        return;
      }
      timer = window.setTimeout(() => setMorphComplete(true), 640 + hs.size * 65 + 300);
    });
    return () => {
      cancelAnimationFrame(raf);
      if (timer) clearTimeout(timer);
    };
  }, [phase]);

  const analyse = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (running.current || !url.trim()) return;
      running.current = true;
      morphed.current = false;
      highlightRefs.current.clear();
      rowRefs.current.clear();
      setPhase("reading");
      setError(null);
      setAnalysis(null);
      setMeta(null);
      setSkeletons([]);
      setClaims([]);
      setChipStates([]);
      setResolvedCount(0);
      setDropped(0);
      setMorphComplete(false);
      setActivity("Fetching the article…");
      try {
        const res = await fetch("/api/analyse", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: url.trim() }),
        });
        if (res.status === 429) throw new Error("You've hit the analysis limit for now — try again in a few minutes.");
        if (!res.ok || !res.body) throw new Error("Something went wrong starting the analysis — try again shortly.");
        for await (const { event, data } of sseEvents(res)) {
          const d = data as Record<string, unknown>;
          if (event === "fetched") {
            setMeta({ title: (d.title as string) ?? null, source: (d.source as string) ?? "", date: (d.date as string) ?? null });
            setActivity("Reading the article and pulling out every claim it makes…");
          } else if (event === "extracted") {
            const facts = ((d.facts as Skeleton[]) ?? []).map((f) => ({ ...f, position: f.position ?? 0 }));
            setSkeletons(facts);
            setClaims(new Array(facts.length).fill(undefined));
            setChipStates(new Array(facts.length).fill("queued"));
            setDropped((d.dropped as number) ?? 0);
            setActivity("");
          } else if (event === "searching") {
            const n = (d.claims as number) ?? 0;
            setActivity(`Checking ${n} claim${n === 1 ? "" : "s"} against independent reporting — this can take a minute…`);
          } else if (event === "checking") {
            const idx = d.index as number;
            setChipStates((prev) => {
              const next = prev.slice();
              next[idx] = "checking";
              return next;
            });
          } else if (event === "claim") {
            const idx = d.index as number;
            setClaims((prev) => {
              const next = prev.slice();
              next[idx] = d.claim as PublicClaim;
              return next;
            });
            setResolvedCount((c) => c + 1);
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
        setPhase((p) => (p === "done" ? p : "error"));
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

  const busy = phase === "reading" || phase === "weighing";
  const showMini = (phase === "reading" || phase === "weighing") && skeletons.length > 0;
  const showList = phase === "weighing" || phase === "done";
  const listSkeletons = analysis
    ? analysis.claims.map((c) => ({ text: c.text, speaker: c.speaker, central: c.central, position: 0 }))
    : skeletons;
  const listClaims: (PublicClaim | undefined)[] = analysis ? analysis.claims : claims;
  const miniFacts: MiniFact[] = skeletons.map((s) => ({ text: s.text, central: s.central, position: s.position }));

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
          disabled={busy}
        />
        <button type="submit" disabled={busy}>
          {busy ? "Analysing…" : "Analyse"}
        </button>
      </form>

      {busy && activity && (
        <div className="activity" role="status">
          <span className="feather-anim" aria-hidden="true">
            ⚖
          </span>
          <span className="activity-text" key={activity}>
            {activity}
          </span>
        </div>
      )}
      {error && <div className="error">{error}</div>}

      <div className="stage" ref={stageRef}>
        {showMini && (
          <MiniPage
            source={meta?.source ?? ""}
            title={meta?.title ?? null}
            facts={miniFacts}
            leaving={phase === "weighing"}
            registerHighlight={(i, el) => {
              if (el) highlightRefs.current.set(i, el);
              else highlightRefs.current.delete(i);
            }}
          />
        )}
        {phase === "weighing" && <Weighing done={resolvedCount} total={skeletons.length} />}
      </div>

      {(meta || analysis) && (phase === "weighing" || phase === "done") && (
        <div className="card article-head">
          <h2>{analysis?.title ?? meta?.title ?? "Untitled article"}</h2>
          <div className="meta">
            {(analysis?.source ?? meta?.source) || ""}
            {(analysis?.date ?? meta?.date) ? ` · ${analysis?.date ?? meta?.date}` : ""}
          </div>
          {analysis?.publisher && (
            <div className={`pub-badge ${analysis.publisher.rated ? "rated" : ""}`}>
              {analysis.publisher.rated ? (
                <>
                  Publisher track record: <strong>{analysis.publisher.score}/100</strong>
                </>
              ) : analysis.publisher.review_started ? (
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
            {!analysis.overall.forecast_only && <span className={`overall-num band-${analysis.overall.band}`}>{analysis.overall.score}</span>}
            <span className={`band band-${analysis.overall.band}`}>{analysis.overall.label}</span>
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

      {showList && listSkeletons.length > 0 && (
        <section aria-live="polite">
          <h3 className="section">The claims</h3>
          {dropped > 0 && phase === "done" && (
            <p className="dropped-note">
              {dropped} snippet{dropped === 1 ? "" : "s"} discarded — could not be verified against the page text.
            </p>
          )}
          <div className="card" style={{ opacity: phase === "weighing" && !morphComplete ? 0 : 1, transition: "opacity .45s ease" }}>
            {listSkeletons.map((s, i) => (
              <ClaimRow
                key={i}
                skeleton={s}
                claim={listClaims[i]}
                state={chipStates[i] ?? "queued"}
                rowRef={(el) => {
                  if (el) rowRefs.current.set(i, el);
                  else rowRefs.current.delete(i);
                }}
              />
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
                <div className="score-col">
                  <span className="big-score muted">—</span>
                </div>
                <div className="main-col">
                  <p className="text">{p.text}</p>
                  {p.speaker && <span className="speaker">— attributed to {p.speaker}</span>}
                  <span className="verdict tier-none">{p.verdict}</span>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {analysis && <ShareCard analysis={analysis} />}
    </>
  );
}
