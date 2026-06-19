import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server bundle for the container image (Caddy → 127.0.0.1 at the #292 cutover).
  output: "standalone",
  // Pin the workspace root to this app so a stray lockfile elsewhere on disk can't be
  // mistaken for the root (file tracing for the standalone output).
  turbopack: { root: __dirname },
  // Dev proxy: the browser calls same-origin /console/api/* (so the cookie rides along, no CORS);
  // when CONSOLE_API_ORIGIN is set we forward it to the FastAPI command/query API (#304). In prod
  // Caddy routes /console/api/* to FastAPI directly after the #292 cutover, so leave it unset there.
  async rewrites() {
    const origin = process.env.CONSOLE_API_ORIGIN;
    if (!origin) return [];
    return [{ source: "/console/api/:path*", destination: `${origin}/console/api/:path*` }];
  },
};

export default nextConfig;
