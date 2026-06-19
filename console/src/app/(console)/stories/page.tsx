import type { Metadata } from "next";

import { RoomPlaceholder } from "@/components/shell/room-placeholder";

export const metadata: Metadata = { title: "Stories" };

export default function StoriesPage() {
  return <RoomPlaceholder id="stories" />;
}
