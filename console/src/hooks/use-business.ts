"use client";

import { useQuery } from "@tanstack/react-query";

import { getAcquisition, getSpend } from "@/lib/api";

/** Estimated spend — LLM by stage/model + provider (acquisition) cost. */
export function useSpend() {
  return useQuery({ queryKey: ["spend"], queryFn: ({ signal }) => getSpend(signal) });
}

/** The acquisition funnel (event-sourced from the marketing site). */
export function useAcquisition() {
  return useQuery({ queryKey: ["acquisition"], queryFn: ({ signal }) => getAcquisition(signal) });
}
