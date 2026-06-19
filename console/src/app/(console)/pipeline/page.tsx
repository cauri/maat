import type { Metadata } from "next";

import { RoomPlaceholder } from "@/components/shell/room-placeholder";

export const metadata: Metadata = { title: "Pipeline" };

export default function PipelinePage() {
  return <RoomPlaceholder id="pipeline" />;
}
