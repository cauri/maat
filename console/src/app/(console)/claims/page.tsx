import type { Metadata } from "next";

import { ClaimsTable } from "@/components/claims/claims-table";

export const metadata: Metadata = { title: "Claims" };

export default function ClaimsPage() {
  return <ClaimsTable />;
}
