"use client";

import { useQuery } from "@tanstack/react-query";

import { getClaim, getClaims } from "@/lib/api";

/** The Claims list — the article→claim firehose (operator-only; never the reader feed). */
export function useClaims(limit = 200) {
  return useQuery({
    queryKey: ["claims", limit],
    queryFn: ({ signal }) => getClaims({ limit }, signal),
  });
}

/** One claim's full provenance (evidence span, relay chain, its cluster) for the inspector. */
export function useClaim(claimId: string | null) {
  return useQuery({
    queryKey: ["claim", claimId],
    queryFn: ({ signal }) => getClaim(claimId as string, signal),
    enabled: claimId != null,
  });
}
