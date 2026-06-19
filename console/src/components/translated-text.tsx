"use client";

import { Languages } from "lucide-react";

import { isEnglish, useTranslate } from "@/hooks/use-translate";
import { cn } from "@/lib/utils";

/**
 * Renders text with an inline English gloss when it's in a foreign language (#54, cauri). The
 * operator always sees the original plus a translated read; English text renders unchanged with
 * no extra request. Display-only — translations are never scored (§4).
 */
export function TranslatedText({
  text,
  language,
  className,
  glossClassName,
}: {
  text: string;
  language?: string | null;
  className?: string;
  glossClassName?: string;
}) {
  const foreign = !isEnglish(language);
  const { data, isFetching } = useTranslate(text, language);
  const gloss = data?.text && data.text !== text ? data.text : null;

  return (
    <span className="flex flex-col gap-0.5">
      <span className={className}>{text}</span>
      {foreign && (gloss || isFetching) && (
        <span className={cn("flex items-start gap-1 text-xs text-muted-foreground", glossClassName)}>
          <Languages className="mt-0.5 size-3 shrink-0 opacity-70" />
          <span>{gloss ?? "translating…"}</span>
        </span>
      )}
    </span>
  );
}
