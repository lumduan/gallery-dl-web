export type Platform = "instagram" | "facebook";

export function detectPlatform(url: string): Platform | null {
  const u = url.toLowerCase();
  if (u.includes("instagram.com")) return "instagram";
  if (u.includes("facebook.com") || u.includes("fb.com") || u.includes("fb.watch"))
    return "facebook";
  return null;
}
