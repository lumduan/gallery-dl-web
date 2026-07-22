import type { NextRequest } from "next/server";

// Catch-all API proxy: forwards /api/* to the FastAPI backend, reading BACKEND_URL at REQUEST time
// (unlike next.config rewrites, which are baked at build time). The more specific /api/health route
// takes precedence over this catch-all, so the frontend's own healthcheck is served locally.
//
// Streaming: the response body (including SSE) is piped through untouched, with no-cache /
// X-Accel-Buffering: no so proxies don't buffer the event stream.

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

type Ctx = { params: Promise<{ path: string[] }> };

async function proxy(req: NextRequest, ctx: Ctx): Promise<Response> {
  const { path } = await ctx.params;
  const url = new URL(req.url);
  const target = `${BACKEND_URL}/api/${path.join("/")}${url.search}`;

  const headers: Record<string, string> = {};
  const ct = req.headers.get("content-type");
  if (ct) headers["content-type"] = ct;
  const accept = req.headers.get("accept");
  if (accept) headers["accept"] = accept;

  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  const upstream = await fetch(target, {
    method: req.method,
    headers,
    body: hasBody ? await req.arrayBuffer() : undefined,
  });

  const respHeaders = new Headers();
  const upstreamCt = upstream.headers.get("content-type");
  if (upstreamCt) respHeaders.set("content-type", upstreamCt);
  // Forward download metadata so files keep their real names (e.g. profile .zip -> "<name>.zip",
  // not the URL's last segment "zip"). Without this the browser falls back to "zip.zip".
  const cd = upstream.headers.get("content-disposition");
  if (cd) respHeaders.set("content-disposition", cd);
  const cl = upstream.headers.get("content-length");
  if (cl) respHeaders.set("content-length", cl);
  respHeaders.set("cache-control", "no-cache");
  respHeaders.set("x-accel-buffering", "no");

  return new Response(upstream.body, { status: upstream.status, headers: respHeaders });
}

export const GET = (req: NextRequest, ctx: Ctx) => proxy(req, ctx);
export const POST = (req: NextRequest, ctx: Ctx) => proxy(req, ctx);
export const PUT = (req: NextRequest, ctx: Ctx) => proxy(req, ctx);
export const DELETE = (req: NextRequest, ctx: Ctx) => proxy(req, ctx);
export const OPTIONS = () => new Response(null, { status: 204 });
