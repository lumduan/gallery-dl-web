"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { detectPlatform } from "@/lib/platform";
import { createJob, getSettings, type SettingsResponse } from "@/lib/api";

export function UrlForm() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [include, setInclude] = useState("posts,reels");
  const [videos, setVideos] = useState(true);
  const [anonymous, setAnonymous] = useState(false);
  const [cookieStatus, setCookieStatus] = useState<SettingsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Presence booleans only — the API never returns cookie values. Used to tell the user *before*
  // they submit that this job will fall back to anonymous, rather than leaving them to infer it
  // from the result.
  useEffect(() => {
    getSettings()
      .then(setCookieStatus)
      .catch(() => {});
  }, []);

  const platform = detectPlatform(url);
  const hasCookies =
    platform === "instagram"
      ? cookieStatus?.has_ig
      : platform === "facebook"
        ? cookieStatus?.has_fb
        : undefined;
  // Only once we know: `undefined` means no platform detected yet, or settings still loading.
  const willFallBack = hasCookies === false && !anonymous;

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
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
      // Only sent when opted in. Omitted, the backend still runs anonymously if no cookies are
      // stored for the platform — this flag is the "even though I have cookies" override.
      if (anonymous) {
        options["anonymous"] = true;
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

        <div className="flex flex-col gap-1">
          <label className="label cursor-pointer justify-start gap-3">
            <input
              type="checkbox"
              className="checkbox checkbox-sm"
              checked={anonymous}
              onChange={(e) => setAnonymous(e.target.checked)}
            />
            <span className="label-text">Anonymous — don&apos;t use my cookies</span>
          </label>
          <p className="pl-9 text-xs text-base-content/60">
            {anonymous
              ? "Public profiles only. Instagram stories, highlights and saved posts are skipped, and Instagram is less reliable logged-out than Facebook."
              : willFallBack
                ? `No ${platform === "instagram" ? "Instagram" : "Facebook"} cookies stored — this job will run anonymously (public content only).`
                : "Leave off to use the cookies stored in Settings. Tick it to spare that session on a public profile."}
          </p>
        </div>

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
          Cookies are optional — without them, public profiles still download. Add them in{" "}
          <a className="link" href="/settings">
            Settings
          </a>{" "}
          to reach private or restricted content, and for more reliable Instagram results.
        </p>
      </div>
    </form>
  );
}
