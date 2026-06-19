import type { Metadata } from "next";

import { FeedbackRoom } from "@/components/feedback/feedback-room";

export const metadata: Metadata = { title: "Feedback" };

export default function FeedbackPage() {
  return <FeedbackRoom />;
}
