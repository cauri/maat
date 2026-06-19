"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { getClaim, getClaims } from "@/lib/api";

const PAGE = 50;

/** The Claims list — the article→claim firehose, lazily paged for infinite scroll. */
export function useClaims() {
  const q = useInfiniteQuery({
    queryKey: ["claims"],
    queryFn: ({ pageParam, signal }) => getClaims({ limit: PAGE, offset: pageParam }, signal),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => {
      const loaded = pages.reduce((n, p) => n + p.claims.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
  });
  return {
    rows: q.data?.pages.flatMap((p) => p.claims) ?? [],
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

/** One claim's full provenance (evidence span, relay chain, its cluster) for the inspector. */
export function useClaim(claimId: string | null) {
  return useQuery({
    queryKey: ["claim", claimId],
    queryFn: ({ signal }) => getClaim(claimId as string, signal),
    enabled: claimId != null,
  });
}
