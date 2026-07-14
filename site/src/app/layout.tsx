import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Maat — weigh the news",
  description:
    "Paste a link to any news article. Maat breaks it into its claims and checks each one against independent reporting.",
  // Brand favicon = the Feather of Maat (gold tile). Assets live in site/public and are served on
  // the apex for BOTH surfaces (see deploy/Caddyfile @site). SVG for modern browsers, .ico as the
  // universal fallback, apple-touch-icon (full-bleed square) for iOS home-screen.
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "any" },
      { url: "/icon.svg", type: "image/svg+xml" },
    ],
    apple: "/apple-touch-icon.png",
  },
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
