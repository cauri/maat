"use client";

import { useQuery } from "@tanstack/react-query";

import { getGraph } from "@/lib/api";

/** The corroboration graph — clusters (facts) and the independent sources that report them. */
export function useGraph() {
  return useQuery({ queryKey: ["graph"], queryFn: ({ signal }) => getGraph(60, signal) });
}
