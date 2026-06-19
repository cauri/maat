"use client";

import { useQuery } from "@tanstack/react-query";

import { getAudit } from "@/lib/api";

/** The authoritative audit history (folded admin.* events). Fetched while the drawer is open. */
export function useAudit(enabled: boolean) {
  return useQuery({
    queryKey: ["audit"],
    queryFn: ({ signal }) => getAudit(100, signal),
    enabled,
    refetchInterval: enabled ? 30_000 : false,
  });
}
