"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { ApiError, getFeedback, triageFeedback } from "@/lib/api";

/** The feedback triage queue (submitted reader feedback awaiting an operator decision). */
export function useFeedback() {
  return useQuery({ queryKey: ["feedback"], queryFn: ({ signal }) => getFeedback(signal) });
}

/** Triage a feedback item — an audited feedback.triaged event. */
export function useTriageFeedback() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ itemId, category, route }: { itemId: string; category: string; route: string }) =>
      triageFeedback(itemId, { category, route, reason: `triaged: ${category}` }),
    onSuccess: (_r, v) => {
      toast.success("Triaged", { description: `${v.itemId} → ${v.category}` });
      qc.invalidateQueries({ queryKey: ["feedback"] });
    },
    onError: (err) =>
      toast.error("Couldn't triage", {
        description: err instanceof ApiError ? err.message : "Unexpected error",
      }),
  });
}
