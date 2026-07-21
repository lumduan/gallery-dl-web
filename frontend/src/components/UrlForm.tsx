"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { detectPlatform } from "@/lib/platform";
import { createJob } from "@/lib/api";

export function UrlForm() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [include, setInclude] = useState("posts,reels");
  const [videos, setVideos] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const platform = detectPlatform(url);
    if (!platform) {
      setError("Enter an Instagram (instagram.com) or Facebook (facebook.com) URL.");
      return;
    }
    setBusy(true);
    try {
      const options: Record<string, unknown> = {};
      if (platform === "instagram") {
        options["include"] = include;
        options["videos"] = videos;
      }
      const { job_id } = await createJob(url, platform, options);
      router.push(`/jobs/${job_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start download");
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="card bg-base-100 shadow-xl border border-base-300">
      <div className="card-body gap-4">
        <h2 className="card-title">Download from a URL</h2>
        <label className="form-control">
          <div className="label">
            <span className="label-text">Instagram or Facebook URL</span>
          </div>
          <input
            type="url"
            className="input input-bordered w-full"
            placeholder="https://www.instagram.com/p/Cxxxx/"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            required
            autoFocus
          />
        </label>

        {error && <div className="alert alert-error py-2 text-sm">{error}</div>}

        <div className="collapse collapse-arrow bg-base-200">
          <input
            type="checkbox"
            checked={showAdvanced}
            onChange={(e) => setShowAdvanced(e.target.checked)}
          />
          <div className="collapse-title font-medium">Advanced options (Instagram)</div>
          <div className="collapse-content flex flex-col gap-3 pt-2">
            <label className="form-control">
              <div className="label">
                <span className="label-text">include</span>
              </div>
              <input
                className="input input-bordered w-full"
                value={include}
                onChange={(e) => setInclude(e.target.value)}
                placeholder="posts,reels,stories,highlights"
              />
            </label>
            <label className="label cursor-pointer justify-start gap-3">
              <input
                type="checkbox"
                className="checkbox checkbox-sm"
                checked={videos}
                onChange={(e) => setVideos(e.target.checked)}
              />
              <span className="label-text">Download videos</span>
            </label>
          </div>
        </div>

        <div className="card-actions justify-end">
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "Starting…" : "Download"}
          </button>
        </div>
        <p className="text-xs text-base-content/60">
          Cookies are required (gallery-dl disables password login). Configure them in{" "}
          <a className="link" href="/settings">
            Settings
          </a>
          .
        </p>
      </div>
    </form>
  );
}
