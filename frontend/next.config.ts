import type { NextConfig } from "next";

const backendUrl = process.env.BACKEND_URL || "http://localhost:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  // Proxy API + health to the FastAPI backend. Runs in the afterFiles phase, so this app's own
  // routes (e.g. /api/health) take precedence; unmatched /api/* are forwarded to the backend.
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${backendUrl}/api/:path*` },
      { source: "/health", destination: `${backendUrl}/health` },
    ];
  },
};

export default nextConfig;
