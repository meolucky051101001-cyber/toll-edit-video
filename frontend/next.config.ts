import type { NextConfig } from "next";
const config: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8010/api/:path*",
      },
    ];
  },
  devIndicators: false,
  experimental: { proxyTimeout: 100000 },
};
export default config;
