"use client";

import { useQuery } from "@tanstack/react-query";

import { translate } from "@/lib/api";

/** Whether a language tag is English (and so needs no gloss). Empty/unknown ⇒ treated as English. */
export function isEnglish(language?: string | null): boolean {
  if (!language) return true;
  return /^en(g(lish)?)?$/i.test(language.trim());
}

/**
 * Display-only English gloss for foreign text (#54). Only fires for non-English text; translations
 * are cached indefinitely (they don't change) and deduped by text across the session.
 */
export function useTranslate(text: string, language?: string | null) {
  const enabled = !isEnglish(language) && text.trim().length > 0;
  return useQuery({
    queryKey: ["translate", text],
    queryFn: ({ signal }) => translate(text, language ?? undefined),
    enabled,
    staleTime: Infinity,
    gcTime: Infinity,
    retry: 1,
  });
}
