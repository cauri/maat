import type { Metadata } from "next";

import { PipelineDashboard } from "@/components/pipeline/pipeline-dashboard";

export const metadata: Metadata = { title: "Pipeline" };

export default function PipelinePage() {
  return <PipelineDashboard />;
}
