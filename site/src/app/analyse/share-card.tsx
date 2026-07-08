"use client";

import { useState, useSyncExternalStore } from "react";
import type { Analysis } from "@/lib/types";

// The share affordance for a completed analysis (P14, #365): a preview of the generated card plus
// platform-appropriate actions. Reality per platform (see serving/analyse.py share_copy):
//   • Twitter/X — intent URL pre-fills our tailored text; the link unfurls the landscape card.
//   • Facebook / LinkedIn — the dialogs only take the URL and read our OG tags, so the card +
//     og:description do the talking; we also offer a "copy caption" for a tailored post body.
//   • Instagram — no web share at all: download the square card + copy the caption (link-in-bio),
//     or, on mobile, hand the image straight to the native share sheet.
// On mobile the Web Share API is offered first — one native sheet reaches every app incl. IG.

const NOOP = () => {};

// Read the client-only Web Share capability without a setState-in-effect (SSR-safe: false on the
// server, true after hydration where navigator.share exists).
function useNativeShare(): boolean {
  return useSyncExternalStore(
    () => NOOP,
    () => typeof navigator !== "undefined" && typeof navigator.share === "function",
    () => false,
  );
}

function pageUrlFor(id: string): string {
  const origin = typeof window !== "undefined" ? window.location.origin : "https://maat.press";
  return `${origin}/analyse?id=${encodeURIComponent(id)}`;
}

function Copyable({ label, text }: { label: string; text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="share-btn"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1800);
        } catch {
          /* clipboard blocked — no-op */
        }
      }}
    >
      {done ? "Copied ✓" : label}
    </button>
  );
}

export default function ShareCard({ analysis }: { analysis: Analysis }) {
  const canNativeShare = useNativeShare();
  const [busy, setBusy] = useState(false);

  const id = analysis.analysis_id;
  const s = analysis.share;
  // Relative URLs resolve against the current origin in the browser — no origin state needed.
  const landscape = `/card?id=${encodeURIComponent(id)}&format=landscape`;
  const square = `/card?id=${encodeURIComponent(id)}&format=square`;

  function openPopup(build: (pageUrl: string) => string) {
    window.open(build(pageUrlFor(id)), "_blank", "noopener,noreferrer,width=600,height=640");
  }

  const twitter = (u: string) =>
    `https://twitter.com/intent/tweet?text=${encodeURIComponent(s.twitter_text)}&url=${encodeURIComponent(u)}`;
  const facebook = (u: string) => `https://www.facebook.com/sharer/sharer.php?u=${encodeURIComponent(u)}`;
  const linkedin = (u: string) => `https://www.linkedin.com/sharing/share-offsite/?url=${encodeURIComponent(u)}`;

  async function nativeShare() {
    setBusy(true);
    try {
      const pageUrl = pageUrlFor(id);
      try {
        const blob = await (await fetch(square)).blob();
        const file = new File([blob], `maat-${id}.png`, { type: "image/png" });
        if (navigator.canShare?.({ files: [file] })) {
          await navigator.share({ files: [file], title: s.og_title, text: s.instagram_caption });
          return;
        }
      } catch {
        /* fall through to a text+link share */
      }
      await navigator.share({ title: s.og_title, text: s.twitter_text, url: pageUrl });
    } catch {
      /* user cancelled or share failed — no-op */
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="share" aria-label="Share this analysis">
      <h3 className="section">Share the ruling</h3>
      <div className="card share-card">
        {/* Our own dynamic PNG — a plain img is correct here (no next/image optimisation wanted). */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img className="share-preview" src={landscape} alt="Maat verdict card for this article" width={1200} height={630} />

        {canNativeShare && (
          <button type="button" className="share-btn primary wide" disabled={busy} onClick={nativeShare}>
            {busy ? "Opening…" : "Share…"}
          </button>
        )}

        <div className="share-row">
          <button type="button" className="share-btn x" onClick={() => openPopup(twitter)}>Post on X</button>
          <button type="button" className="share-btn li" onClick={() => openPopup(linkedin)}>LinkedIn</button>
          <button type="button" className="share-btn fb" onClick={() => openPopup(facebook)}>Facebook</button>
          <Copyable label="Copy link" text={pageUrlFor(id)} />
        </div>

        <div className="share-block">
          <div className="share-block-head">
            <strong>Instagram</strong>
            <span>no link sharing — post the image, caption points to your bio</span>
          </div>
          <div className="share-row">
            <a className="share-btn" href={square} download={`maat-${id}.png`}>Download image</a>
            <Copyable label="Copy caption" text={s.instagram_caption} />
          </div>
        </div>

        <details className="share-more">
          <summary>Copy a post for LinkedIn / Facebook</summary>
          <p className="share-caption">{s.linkedin_text}</p>
          <Copyable label="Copy post text" text={s.linkedin_text} />
        </details>
      </div>
    </section>
  );
}
