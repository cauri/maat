import type { Metadata } from "next";

import { RoomPlaceholder } from "@/components/shell/room-placeholder";

export const metadata: Metadata = { title: "Feedback" };

export default function FeedbackPage() {
  return <RoomPlaceholder id="feedback" />;
}
