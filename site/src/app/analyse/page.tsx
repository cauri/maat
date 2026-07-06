import type { Metadata } from "next";
import { Suspense } from "react";
import Analyser from "./analyser";

export const metadata: Metadata = {
  title: "Maat — weigh the news",
  description:
    "Paste a link to any news article. Maat breaks it into its claims, checks each one against independent reporting, and gives the article one clear read.",
};

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
            Paste a link to a news article. Maat breaks it into its claims and checks each one
            against independent reporting.
          </p>
          <Suspense>
            <Analyser />
          </Suspense>
        </section>
        <footer className="foot">
          Maat measures whether an article&rsquo;s factual claims hold up against independent
          reporting — not its tone, its bias, or what it leaves out. Surprising claims are held
          to a higher bar than everyday ones. ·{" "}
          <a href="https://www.maat.press/privacy">Privacy</a>
        </footer>
      </main>
    </>
  );
}
