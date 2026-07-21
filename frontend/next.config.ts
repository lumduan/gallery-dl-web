import type { NextConfig } from "next";

// NOTE: /api/* is proxied to the backend by a catch-all route handler
// (src/app/api/[...path]/route.ts), which reads BACKEND_URL at REQUEST time. We deliberately do
// NOT use next.config `rewrites()` here — those are resolved at build time, which would bake in
// whatever BACKEND_URL (or the default) was present during `next build`.

const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
};

export default nextConfig;
