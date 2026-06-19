import type { Metadata } from "next";

import { SourcesTable } from "@/components/sources/sources-table";

export const metadata: Metadata = { title: "Sources" };

export default function SourcesPage() {
  return <SourcesTable />;
}
