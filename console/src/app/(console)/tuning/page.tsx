import type { Metadata } from "next";

import { TuningRoom } from "@/components/tuning/tuning-room";

export const metadata: Metadata = { title: "Tuning" };

export default function TuningPage() {
  return <TuningRoom />;
}
