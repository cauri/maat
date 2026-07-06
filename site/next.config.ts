import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server bundle for the container image (Caddy → 127.0.0.1:3200).
  output: "standalone",
  // Pin the workspace root to this app so a stray lockfile elsewhere on disk can't be
  // mistaken for the root (file tracing for the standalone output).
  turbopack: { root: __dirname },
  // Dev proxy: the browser calls same-origin /api/analyse* (no CORS); in production Caddy
  // rewrites those to the reader's /api/v2/analyse*. Set SITE_API_ORIGIN=http://localhost:8000
  // for local dev against a running reader; leave it unset in the container.
  async rewrites() {
    const origin = process.env.SITE_API_ORIGIN;
    if (!origin) return [];
    return [
      { source: "/api/analyse", destination: `${origin}/api/v2/analyse` },
      { source: "/api/analyse/:path*", destination: `${origin}/api/v2/analyse/:path*` },
    ];
  },
};

export default nextConfig;
