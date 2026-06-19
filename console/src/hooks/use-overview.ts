"use client";

import { useQuery } from "@tanstack/react-query";

import { getOverview } from "@/lib/api";

/** The Overview landing snapshot — counts, clocks, freshness. Polls so the dashboard stays live. */
export function useOverview() {
  return useQuery({
    queryKey: ["overview"],
    queryFn: ({ signal }) => getOverview(signal),
    refetchInterval: 30_000,
  });
}
