/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',   // required for multi-stage Docker build
  async rewrites() {
    return [
      // Proxy all /api/* requests to FastAPI backend in development
      // NOTE: 127.0.0.1 (not localhost) — on Windows 'localhost' resolves to IPv6 ::1
      // but uvicorn binds 0.0.0.0 (IPv4 only) → ECONNREFUSED. 127.0.0.1 forces IPv4.
      {
        source:      '/api/:path*',
        destination: `${process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8000'}/api/:path*`,
      },
      {
        source:      '/health',
        destination: `${process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8000'}/health`,
      },
    ];
  },
};

module.exports = nextConfig;
