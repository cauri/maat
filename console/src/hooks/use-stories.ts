"use client";

import { useQuery } from "@tanstack/react-query";

import { getStories, getStory } from "@/lib/api";

/** The Stories list — one credibility roll-up per story (the same the app's feed shows). */
export function useStories() {
  return useQuery({
    queryKey: ["stories"],
    queryFn: ({ signal }) => getStories({ limit: 200 }, signal),
  });
}

/** One story's full transparent breakdown (facts, forecasts, trajectory) for the workspace. */
export function useStory(nodeId: string | null) {
  return useQuery({
    queryKey: ["story", nodeId],
    queryFn: ({ signal }) => getStory(nodeId as string, signal),
    enabled: nodeId != null,
  });
}
