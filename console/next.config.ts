import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server bundle for the container image (Caddy → 127.0.0.1 at the #292 cutover).
  output: "standalone",
  // Pin the workspace root to this app so a stray lockfile elsewhere on disk can't be
  // mistaken for the root (file tracing for the standalone output).
  turbopack: { root: __dirname },
};

export default nextConfig;
