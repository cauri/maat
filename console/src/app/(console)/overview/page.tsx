import type { Metadata } from "next";

import { RoomPlaceholder } from "@/components/shell/room-placeholder";

export const metadata: Metadata = { title: "Overview" };

export default function OverviewPage() {
  return <RoomPlaceholder id="overview" />;
}
