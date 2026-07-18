"use client";

import Link from "next/link";

import { ConfigKnobs } from "./config-knobs";

// Prompts moved to their own room (#446) — /prompts in the rail. cauri's cross-project pattern is
// a first-class prompt hub, and a tab inside "Tuning" is exactly the buried placement that kept
// it from being found. Tuning keeps the config knobs.
export function TuningRoom() {
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4 p-4 sm:p-6">
      <p className="text-sm text-muted-foreground">
        Scoring settings. Propose stages a value to review; Promote makes it live — which needs
        your sign-off. Looking for the prompts? They have their own room now:{" "}
        <Link href="/prompts" className="underline underline-offset-2">
          Prompts
        </Link>
        .
      </p>
      <ConfigKnobs />
    </div>
  );
}
