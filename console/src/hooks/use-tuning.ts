"use client";

import { useQuery } from "@tanstack/react-query";

import { getConfig, getPrompt, getPrompts } from "@/lib/api";

/** Config knobs — model routing, thresholds, etc. (active vs proposed vs default). */
export function useConfig() {
  return useQuery({ queryKey: ["config"], queryFn: ({ signal }) => getConfig(signal) });
}

/** The editable prompt registry (every runtime + console prompt, incl. Sia's persona). */
export function usePrompts() {
  return useQuery({ queryKey: ["prompts"], queryFn: ({ signal }) => getPrompts(signal) });
}

/** One prompt's current + default text, for the editor. */
export function usePrompt(key: string | null) {
  return useQuery({
    queryKey: ["prompt", key],
    queryFn: ({ signal }) => getPrompt(key as string, signal),
    enabled: key != null,
  });
}
