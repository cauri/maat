import { redirect } from "next/navigation";

// The apex currently serves only /analyse (Caddy routes the rest to the landing page);
// anyone landing on the app root goes straight to the product.
export default function Home() {
  redirect("/analyse");
}
