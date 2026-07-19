import type { Metadata } from "next";
import { Suspense } from "react";
import { loadAnalysis, PUBLIC_ORIGIN } from "@/lib/server";
import Analyser from "./analyser";

const DEFAULT_TITLE = "Maat — weigh the news";
const DEFAULT_DESC =
  "Paste a link to any news article — or type a claim you've heard. Maat checks each claim against independent reporting and primary sources, and traces where it came from.";

// Per-analysis OG/Twitter tags for a shared ?id= link: when a platform crawls the URL it unfurls
// the article's verdict card. (The live tab's client-side ?id= update doesn't re-run this — it
// doesn't need to; only crawlers of the shared URL do.)
export async function generateMetadata({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}): Promise<Metadata> {
  const sp = await searchParams;
  const id = typeof sp.id === "string" ? sp.id : undefined;
  const base: Metadata = { metadataBase: new URL(PUBLIC_ORIGIN), title: DEFAULT_TITLE, description: DEFAULT_DESC };
  if (!id) return base;
  const a = await loadAnalysis(id);
  if (!a) return base;
  const card = `${PUBLIC_ORIGIN}/card?id=${encodeURIComponent(id)}&format=landscape`;
  const pageUrl = `${PUBLIC_ORIGIN}/analyse?id=${encodeURIComponent(id)}`;
  const headline = a.overall.forecast_only ? a.overall.label : `${a.overall.label} · ${a.overall.score}/100`;
  const subject = a.kind === "claim" ? (a.checked?.display ?? "this claim") : (a.title ?? "this article");
  const title = a.share?.og_title ?? `Maat weighed “${subject}”`;
  const description =
    a.share?.og_description ??
    `${headline}. Maat weighs each factual claim against independent reporting — not tone or bias.`;
  return {
    metadataBase: new URL(PUBLIC_ORIGIN),
    title,
    description,
    openGraph: {
      title, description, url: pageUrl, siteName: "Maat", type: "article",
      images: [{ url: card, width: 1200, height: 630, alt: title }],
    },
    twitter: { card: "summary_large_image", title, description, images: [card] },
  };
}

export default function AnalysePage() {
  return (
    <>
      <header className="bar">
        <div className="wrap">
          <a className="mark" href="/analyse">
            <span className="feather" aria-hidden>
              ⚖
            </span>
            Maat
          </a>
          <nav>
            <a href="https://www.maat.press">About</a>
          </nav>
        </div>
      </header>
      <main className="wrap">
        <section className="hero">
          <h1>Weigh the news.</h1>
          <p className="lede">
            Paste a link to a news article — or type a claim you&rsquo;ve heard. Maat checks
            every claim against independent reporting and primary sources, and traces where it
            came from.
          </p>
          <Suspense>
            <Analyser />
          </Suspense>
        </section>
        <footer className="foot">
          Maat measures whether factual claims hold up against independent reporting and primary
          sources — not tone, bias, or who told you. Surprising claims are held to a higher bar
          than everyday ones. · <a href="https://www.maat.press/privacy">Privacy</a>
        </footer>
      </main>
    </>
  );
}
