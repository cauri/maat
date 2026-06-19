import type { Metadata } from "next";

import { RoomPlaceholder } from "@/components/shell/room-placeholder";

export const metadata: Metadata = { title: "Sources" };

export default function SourcesPage() {
  return <RoomPlaceholder id="sources" />;
}
