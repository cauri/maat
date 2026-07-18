import type { Metadata } from "next";

import { PromptsRoom } from "@/components/prompts/prompts-room";

export const metadata: Metadata = { title: "Prompts" };

export default function PromptsPage() {
  return <PromptsRoom />;
}
