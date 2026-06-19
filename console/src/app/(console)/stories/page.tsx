import type { Metadata } from "next";

import { StoriesTable } from "@/components/stories/stories-table";

export const metadata: Metadata = { title: "Stories" };

export default function StoriesPage() {
  return <StoriesTable />;
}
