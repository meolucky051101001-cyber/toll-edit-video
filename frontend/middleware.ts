import { NextResponse, type NextRequest } from "next/server";

/**
 * Injects the shared API token into every proxied /api/* request so the
 * browser client (fetch, EventSource, <img> screenshots) never needs to know
 * the token itself. Set API_TOKEN in .env (loaded by start.ps1); when unset
 * the backend runs without token checks and this middleware is a no-op.
 */
export function middleware(request: NextRequest) {
  const token = process.env.API_TOKEN;
  if (!token) return NextResponse.next();

  const headers = new Headers(request.headers);
  headers.set("x-api-token", token);
  return NextResponse.next({ request: { headers } });
}

export const config = {
  // Only the paths rewritten to the FastAPI backend need the token.
  matcher: ["/api/:path*"],
};
