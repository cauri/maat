import type { Metadata } from "next";

import { RoomPlaceholder } from "@/components/shell/room-placeholder";

export const metadata: Metadata = { title: "Tuning" };

export default function TuningPage() {
  return <RoomPlaceholder id="tuning" />;
}
