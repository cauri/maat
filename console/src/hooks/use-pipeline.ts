"use client";

import { useQuery } from "@tanstack/react-query";

import { getPipeline } from "@/lib/api";

/** Pipeline health & ops — stages, throughput, calibration, alerts. Polls to stay live. */
export function usePipeline() {
  return useQuery({
    queryKey: ["pipeline"],
    queryFn: ({ signal }) => getPipeline(signal),
    refetchInterval: 20_000,
  });
}
