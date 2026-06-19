"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { ApiError, runCommand } from "@/lib/api";

/**
 * Run an operator command (an audited `ADMIN_*` event — the only mutation path, D5/D28) with
 * toast feedback and query invalidation. Shared by every room and, later, by Sia (#306) — the
 * exact same path a human takes.
 */
export function useRunCommand(invalidateKeys: unknown[][] = []) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, body }: { name: string; body: Record<string, unknown> }) =>
      runCommand(name, body),
    onSuccess: (res) => {
      toast.success("Change applied", {
        description: `${res.command} — audited as ${res.event_type}`,
      });
      for (const key of invalidateKeys) qc.invalidateQueries({ queryKey: key });
    },
    onError: (err) => {
      toast.error("Couldn't apply the change", {
        description: err instanceof ApiError ? err.message : "Unexpected error",
      });
    },
  });
}
