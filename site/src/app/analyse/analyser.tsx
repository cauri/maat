"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
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
const EX_RANK: Record<string, number> = { routine: 0, ordinary: 1, notable: 2, significant: 3, extraordinary: 4 };

// Display order (cauri): most significant first, and within a significance tier the least-supported
// (lowest score) first. Checked claims sort first; UNCHECKED claims (#397, never searched, no score)
// sink below them — they carry no verdict, so leading with them read as "corroboration is broken";
// still-weighing claims trail in document order — so each claim rises into its place as it resolves.
function orderedIndices(claims: (PublicClaim | undefined)[]): number[] {
  const idx = claims.map((_, i) => i);
  const checked = idx.filter((i) => claims[i] && claims[i]!.checked);
  const unchecked = idx.filter((i) => claims[i] && !claims[i]!.checked);
  const pending = idx.filter((i) => !claims[i]);
  const byExtremity = (a: number, b: number) => (EX_RANK[claims[b]!.extremity] ?? 2) - (EX_RANK[claims[a]!.extremity] ?? 2);
  checked.sort((a, b) => {
    const e = byExtremity(a, b);
    if (e !== 0) return e;
    const sa = claims[a]!.score ?? 0;
    const sb = claims[b]!.score ?? 0;
    if (sa !== sb) return sa - sb; // least supported first
    return a - b; // stable
  });
  unchecked.sort((a, b) => byExtremity(a, b) || a - b);
  return [...checked, ...unchecked, ...pending];
}

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
  onCheck,
  forcing,
  checkError,
}: {
  skeleton: Skeleton;
  claim?: PublicClaim;
  state: ChipState;
  rowRef: (el: HTMLElement | null) => void;
  onCheck: () => void;
  forcing: boolean;
  checkError?: string;
}) {
  // Three states: still weighing (no claim), NOT CHECKED (#397 — skipped over the cap, no score),
  // and resolved (a real verdict). "Not checked" is never dressed up as a lone single-source claim.
  const notChecked = claim && !claim.checked;
  const smelly = claim && claim.checked && (claim.tier === "floor" || claim.verdict.startsWith("Disputed"));
  return (
    <div ref={rowRef} className={`claim ${claim ? "resolved" : "unresolved"} ${notChecked ? "unchecked" : ""}`}>
      {!claim ? (
        <div className="score-col pending" aria-hidden="true">
          <span className={`weigh-glyph ${state === "checking" ? "busy" : ""}`}>⚖</span>
        </div>
      ) : notChecked ? (
        <div className="score-col" aria-hidden="true">
          {forcing ? (
            <span className="weigh-glyph busy">⚖</span>
          ) : (
            <span className="big-score muted not-checked-mark">–</span>
          )}
        </div>
      ) : (
        <div className="score-col">
          <span className={`big-score tier-${claim.tier}`}>{claim.score}</span>
          <span className="denom">/100</span>
          {smelly && (
            <span className="stink" role="img" aria-label="reads well below the bar">
              💩
            </span>
          )}
        </div>
      )}
      <div className="main-col">
        <p className="text">
          {skeleton.text}
          {skeleton.central && <span className="central-tag">central claim</span>}
        </p>
        {skeleton.speaker && <span className="speaker">— attributed to {skeleton.speaker}</span>}
        {!claim ? (
          <span className="weighing">
            {state === "checking" ? "checking against independent reporting…" : "queued for weighing…"}
            <span className="shimmer" aria-hidden="true" />
          </span>
        ) : notChecked ? (
          <>
            <span className="verdict tier-unchecked">Not checked yet</span>
            <Significance level={claim.extremity} />
            <div className="check-row">
              <button type="button" className="check-btn" onClick={onCheck} disabled={forcing}>
                {forcing ? "Checking against independent reporting…" : "Check this claim"}
              </button>
              {checkError && <span className="check-err">{checkError}</span>}
            </div>
          </>
        ) : (
          <>
            <span className={`verdict tier-${claim.tier}`}>{claim.verdict}</span>
            <Significance level={claim.extremity} />
          </>
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
  const [forcing, setForcing] = useState<Set<number>>(new Set()); // #397 force-checks in flight
  const [checkErrors, setCheckErrors] = useState<Record<number, string>>({});
  const running = useRef(false);
  const morphed = useRef(false);
  const highlightRefs = useRef<Map<number, HTMLElement>>(new Map());
  const rowRefs = useRef<Map<number, HTMLElement>>(new Map());
  const stageRef = useRef<HTMLDivElement>(null);
  const prevTops = useRef<Map<number, number>>(new Map());

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

  // Once the article is read (claims found + highlighted), tip into weighing — but hold the
  // highlights on screen a beat so they read (the mini-page has already been scanning through the
  // fetch, so the reading act has presence rather than flashing by).
  useEffect(() => {
    if (phase !== "reading" || skeletons.length === 0) return;
    const t = setTimeout(() => setPhase("weighing"), 2200);
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
              { transform: "translate(0,0)", opacity: 0.95, offset: 0 },
              { transform: `translate(${dx * 0.5}px, ${dy * 0.5}px)`, opacity: 0.95, offset: 0.55 },
              { transform: `translate(${dx}px, ${dy}px)`, opacity: 0 },
            ],
            { duration: 900, delay: i * 90, easing: "cubic-bezier(.45,0,.15,1)", fill: "forwards" },
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
      timer = window.setTimeout(() => setMorphComplete(true), 900 + hs.size * 90 + 400);
    });
    return () => {
      cancelAnimationFrame(raf);
      if (timer) clearTimeout(timer);
    };
  }, [phase]);

  // FLIP reorder — as claims resolve and the list re-sorts, rows glide to their new slot rather
  // than jumping. Runs after every render; a row only animates if its position actually moved.
  useLayoutEffect(() => {
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    rowRefs.current.forEach((el, i) => {
      const top = el.offsetTop;
      const prev = prevTops.current.get(i);
      if (!reduce && prev !== undefined && Math.abs(prev - top) > 1) {
        try {
          el.animate(
            [{ transform: `translateY(${prev - top}px)` }, { transform: "translateY(0)" }],
            { duration: 380, easing: "cubic-bezier(.5,0,.2,1)" },
          );
        } catch {
          /* no-op */
        }
      }
      prevTops.current.set(i, top);
    });
  });

  const analyse = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (running.current || !url.trim()) return;
      running.current = true;
      morphed.current = false;
      highlightRefs.current.clear();
      rowRefs.current.clear();
      prevTops.current.clear();
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
            setActivity(
              d.deep
                ? `Digging deeper on ${n} claim${n === 1 ? "" : "s"} that came back uncorroborated…`
                : `Checking ${n} claim${n === 1 ? "" : "s"} against independent reporting — this can take a minute…`,
            );
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

  // Force-check ONE "Not checked" claim on demand (#397): run the live path for it and swap the row
  // in place (the overall hero is the batch read and doesn't move — over-cap claims are non-central).
  const forceCheck = useCallback(
    async (i: number, claim: PublicClaim) => {
      const target = analysis?.url ?? url;
      if (!target || claim.checked) return;
      setForcing((prev) => new Set(prev).add(i));
      setCheckErrors((prev) => {
        const n = { ...prev };
        delete n[i];
        return n;
      });
      try {
        const res = await fetch("/api/analyse/check-claim", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: target, text: claim.text, voice: claim.voice, speaker: claim.speaker, central: claim.central }),
        });
        if (res.status === 429) throw new Error("Too many checks just now — try again in a moment.");
        if (!res.ok) throw new Error("Couldn't check this claim — try again shortly.");
        const updated = ((await res.json()) as { claim: PublicClaim }).claim;
        setAnalysis((a) => {
          if (!a) return a;
          const claims = a.claims.slice();
          claims[i] = updated;
          const unchecked = Math.max(0, (a.overall.unchecked ?? 0) - 1);
          return { ...a, claims, overall: { ...a.overall, unchecked } };
        });
        setClaims((prev) => {
          if (!prev.length) return prev;
          const n = prev.slice();
          n[i] = updated;
          return n;
        });
      } catch (e) {
        setCheckErrors((prev) => ({ ...prev, [i]: e instanceof Error ? e.message : "Check failed — try again shortly." }));
      } finally {
        setForcing((prev) => {
          const n = new Set(prev);
          n.delete(i);
          return n;
        });
      }
    },
    [analysis, url],
  );

  const busy = phase === "reading" || phase === "weighing";
  // Show the mini-page from the moment we start reading (a scanning shell during the fetch wait),
  // through the morph; it collapses out (CSS) once weighing begins so it never leaves a gap.
  const showMini = phase === "reading" || phase === "weighing";
  const showList = phase === "weighing" || phase === "done";
  const listSkeletons = analysis
    ? analysis.claims.map((c) => ({ text: c.text, speaker: c.speaker, central: c.central, position: 0 }))
    : skeletons;
  const listClaims: (PublicClaim | undefined)[] = analysis ? analysis.claims : claims;
  const order = useMemo(() => orderedIndices(listClaims), [listClaims]);
  const uncheckedCount = listClaims.filter((c) => c && !c.checked).length;
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
            leaving={phase === "weighing" && morphComplete}
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
          {analysis?.publisher &&
            (analysis.publisher.rated ? (
              <div className="pub-rating">
                <div className="pub-score">
                  <span className="pub-num">{analysis.publisher.score}</span>
                  <span className="pub-denom">%</span>
                </div>
                <div className="pub-meta">
                  <span className="pub-label">Publisher track record</span>
                  <span className="pub-sub">
                    How this outlet&rsquo;s past reporting has held up against independent sources over time.
                  </span>
                </div>
              </div>
            ) : (
              <div className="pub-badge">
                {analysis.publisher.review_started ? (
                  <>No track record yet. Maat has started reviewing this publisher&rsquo;s past reporting for accuracy.</>
                ) : (
                  <>Publisher not yet rated.</>
                )}
              </div>
            ))}
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
          {phase === "done" && uncheckedCount > 0 && (
            <p className="unchecked-note">
              {uncheckedCount === 1
                ? "1 claim wasn’t checked — it fell below the depth this analysis reached. Check it individually below."
                : `${uncheckedCount} claims weren’t checked — they fell below the depth this analysis reached. Check any individually below.`}
            </p>
          )}
          <div className="card" style={{ opacity: phase === "weighing" && !morphComplete ? 0 : 1, transition: "opacity .45s ease" }}>
            {order.map((i) => (
              <ClaimRow
                key={i}
                skeleton={listSkeletons[i]}
                claim={listClaims[i]}
                state={chipStates[i] ?? "queued"}
                rowRef={(el) => {
                  if (el) rowRefs.current.set(i, el);
                  else rowRefs.current.delete(i);
                }}
                onCheck={() => {
                  const c = listClaims[i];
                  if (c) forceCheck(i, c);
                }}
                forcing={forcing.has(i)}
                checkError={checkErrors[i]}
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
