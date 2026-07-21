// Typed wrappers for the backend JSON API. All paths are relative ("/api/..."); Next.js rewrites
// them to the FastAPI backend (see next.config.ts).

export interface JobSummary {
  id: string;
  url: string;
  platform: string;
  status: string;
  created_at: number;
  started_at: number | null;
  ended_at: number | null;
  final_summary: Record<string, unknown> | null;
}

export interface SettingsResponse {
  has_ig: boolean;
  has_fb: boolean;
}

export interface FileEntry {
  path: string;
  name: string;
  size: number;
  mtime: number;
  platform: string;
}

async function asJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = (body as { detail?: string }).detail ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export async function createJob(
  url: string,
  platform?: string | null,
  options?: Record<string, unknown>,
): Promise<{ job_id: string; status: string }> {
  const res = await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, platform, options }),
  });
  return asJson(res);
}

export async function getJob(jobId: string): Promise<JobSummary> {
  return asJson(await fetch(`/api/jobs/${jobId}`));
}

export async function listJobs(): Promise<JobSummary[]> {
  return asJson(await fetch("/api/jobs"));
}

export async function getSettings(): Promise<SettingsResponse> {
  return asJson(await fetch("/api/settings"));
}

export async function updateCookies(body: {
  ig_sessionid?: string;
  fb_cookies_text?: string;
}): Promise<SettingsResponse> {
  const res = await fetch("/api/settings/cookies", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return asJson(res);
}

export async function listFiles(): Promise<{ files: FileEntry[] }> {
  return asJson(await fetch("/api/files"));
}

export function downloadUrl(path: string): string {
  return `/api/files/download?path=${encodeURIComponent(path)}`;
}

export function jobZipUrl(jobId: string): string {
  return `/api/jobs/${jobId}/zip`;
}
