import type { Metadata } from "next";

import { RoomPlaceholder } from "@/components/shell/room-placeholder";

export const metadata: Metadata = { title: "Graph" };

export default function GraphPage() {
  return <RoomPlaceholder id="graph" />;
}
