import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Maat — weigh the news",
  description:
    "Paste a link to any news article. Maat breaks it into its claims and checks each one against independent reporting.",
  openGraph: {
    title: "Maat — weigh the news",
    description:
      "Paste a link to any news article. Maat breaks it into its claims and checks each one against independent reporting.",
    type: "website",
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
