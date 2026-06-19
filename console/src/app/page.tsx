import { redirect } from "next/navigation";

/** The console opens on the Overview room. */
export default function RootPage() {
  redirect("/overview");
}
