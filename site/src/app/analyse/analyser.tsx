"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// ── the public wire types (serving/analyse.py — verdicts only, never the mechanism) ────────────

type PublicClaim = {
  text: string;
  voice: "own" | "attributed";
  speaker: string | null;
  central: boolean;
  extremity: string;
  score: number;
  verdict: string;
  tier: "hi" | "mid" | "lo" | "floor" | "none";
};

type PublicProjection = { text: string; speaker: string | null; verdict: string };

type Analysis = {
  analysis_id: string;
  url: string;
  source: string;
  title: string | null;
  date: string | null;
  publisher: { domain: string; rated: boolean; score: number | null };
  overall: {
    score: number;
    band: string;
    label: string;
    reasons: string[];
    capped: boolean;
    forecast_only: boolean;
  };
  claims: PublicClaim[];
  projections: PublicProjection[];
  scope: string;
};

type Skeleton = { text: string; speaker: string | null; central: boolean };

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

// ── presentation helpers ────────────────────────────────────────────────────────────────────────

function ExtremityDots({ level }: { level: string }) {
  const n = EXTREMITY_LEVELS.indexOf(level) + 1;
  return (
    <span className="extremity" title={`How surprising this claim is: ${level}. The more extraordinary a claim, the more independent confirmation Maat requires.`}>
      {level}
      <span className="dots" aria-hidden>
        {EXTREMITY_LEVELS.map((_, i) => (
          <i key={i} className={i < n ? "on" : ""} />
        ))}
      </span>
    </span>
  );
}

function ClaimRow({ claim, skeleton }: { claim?: PublicClaim; skeleton: Skeleton }) {
  const c = claim;
  return (
    <div className="claim">
      <div className="top">
        {c ? <ExtremityDots level={c.extremity} /> : <span className="extremity">weighing…</span>}
        {skeleton.central && <span className="central-tag">central claim</span>}
      </div>
      <p className="text">{skeleton.text}</p>
      {skeleton.speaker && <span className="speaker">— attributed to {skeleton.speaker}</span>}
      <div className="verdict-row">
        {c ? (
          <span className={`verdict tier-${c.tier}`}>
            {c.verdict} <span className="vscore">· {c.score}/100</span>
          </span>
        ) : (
          <span className="weighing">checking against independent reporting…</span>
        )}
      </div>
    </div>
  );
}

// ── the page ────────────────────────────────────────────────────────────────────────────────────

export default function Analyser() {
  const [url, setUrl] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [status, setStatus] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [meta, setMeta] = useState<{ title: string | null; source: string; date: string | null } | null>(null);
  const [skeletons, setSkeletons] = useState<Skeleton[]>([]);
  const [claims, setClaims] = useState<(PublicClaim | undefined)[]>([]);
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
      setStatus("Fetching the article…");
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
            setStatus("Reading the article and extracting its claims…");
          } else if (event === "extracted") {
            const facts = (d.facts as Skeleton[]) ?? [];
            setSkeletons(facts);
            setClaims(new Array(facts.length).fill(undefined));
            setStatus(`${facts.length} factual claim${facts.length === 1 ? "" : "s"} found — weighing each one…`);
          } else if (event === "searching") {
            setStatus("Checking claims against independent reporting — this can take a minute…");
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
            setStatus("");
            window.history.replaceState(null, "", `?id=${encodeURIComponent(a.analysis_id)}`);
          } else if (event === "error") {
            throw new Error((d.detail as string) || "Analysis failed — try again shortly.");
          }
        }
        setPhase((p) => (p === "running" ? "error" : p));
      } catch (err) {
        setPhase("error");
        setStatus("");
        setError(err instanceof Error ? err.message : "Analysis failed — try again shortly.");
      } finally {
        running.current = false;
      }
    },
    [url],
  );

  const shown = analysis
    ? { skeletons: analysis.claims.map((c) => ({ text: c.text, speaker: c.speaker, central: c.central })), claims: analysis.claims as (PublicClaim | undefined)[] }
    : { skeletons, claims };

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
      <p className="status" role="status">
        {status}
      </p>
      {error && <div className="error">{error}</div>}

      {(meta || analysis) && (
        <div className="card article-head">
          <h2>{analysis?.title ?? meta?.title ?? "Untitled article"}</h2>
          <div className="meta">
            {(analysis?.source ?? meta?.source) || ""}
            {(analysis?.date ?? meta?.date) ? ` · ${analysis?.date ?? meta?.date}` : ""}
            {analysis && (
              <>
                {" · publisher track record: "}
                {analysis.publisher.rated ? (
                  <span className="pub-rated">{analysis.publisher.score}/100</span>
                ) : (
                  <span>not yet rated</span>
                )}
              </>
            )}
          </div>
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
          <div className="card">
            {shown.skeletons.map((s, i) => (
              <ClaimRow key={i} skeleton={s} claim={shown.claims[i]} />
            ))}
          </div>
        </section>
      )}

      {analysis && analysis.projections.length > 0 && (
        <section className="projections">
          <h3 className="section">Forecasts &amp; opinions — not scored for truth</h3>
          <div className="card">
            {analysis.projections.map((p, i) => (
              <div className="claim" key={i}>
                <p className="text">{p.text}</p>
                {p.speaker && <span className="speaker">— attributed to {p.speaker}</span>}
                <span className="verdict tier-none">{p.verdict}</span>
              </div>
            ))}
          </div>
        </section>
      )}
    </>
  );
}
