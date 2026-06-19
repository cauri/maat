"use client";

import { useQuery } from "@tanstack/react-query";

import { getSources } from "@/lib/api";

/** The Sources list — one canonical reliability number + trajectory per outlet (#309). */
export function useSources() {
  return useQuery({
    queryKey: ["sources"],
    queryFn: ({ signal }) => getSources(signal),
  });
}
