import type { NextConfig } from "next";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // TypeScript 7 is the Go rewrite and no longer exposes the JS compiler API that
  // Next's type-check worker historically called. Next detects this and offers two
  // ways out: downgrade to TS 6, or drive `tsc` as a CLI. We keep TS 7 for the
  // speed and take the CLI path.
  experimental: { useTypeScriptCli: true },
  // The browser never talks to the API host directly: it calls same-origin /api/*
  // and the web tier proxies. Keeps CORS narrow (PRD §19.5) and means no API
  // credential or internal hostname is ever exposed to the client (§7.1).
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_BASE_URL}/api/:path*` }];
  },
};

export default nextConfig;
