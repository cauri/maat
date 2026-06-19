"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { getStories, getStory } from "@/lib/api";

const PAGE = 50;

/** The Stories list — one credibility roll-up per story, lazily paged for infinite scroll. */
export function useStories() {
  const q = useInfiniteQuery({
    queryKey: ["stories"],
    queryFn: ({ pageParam, signal }) => getStories({ limit: PAGE, offset: pageParam }, signal),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => {
      const loaded = pages.reduce((n, p) => n + p.stories.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
  });
  return {
    rows: q.data?.pages.flatMap((p) => p.stories) ?? [],
    total: q.data?.pages[0]?.total ?? 0,
    isLoading: q.isLoading,
    error: q.error,
    isFetching: q.isFetching,
    refetch: q.refetch,
    fetchNextPage: q.fetchNextPage,
    hasNextPage: q.hasNextPage,
    isFetchingNextPage: q.isFetchingNextPage,
  };
}

/** One story's full transparent breakdown (facts, forecasts, trajectory) for the workspace. */
export function useStory(nodeId: string | null) {
  return useQuery({
    queryKey: ["story", nodeId],
    queryFn: ({ signal }) => getStory(nodeId as string, signal),
    enabled: nodeId != null,
  });
}
