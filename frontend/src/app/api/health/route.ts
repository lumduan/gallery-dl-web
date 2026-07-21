import { NextResponse } from "next/server";

// Lightweight health endpoint for the Dockerfile HEALTHCHECK (matches the house convention).
export async function GET() {
  return NextResponse.json({ status: "ok" });
}
