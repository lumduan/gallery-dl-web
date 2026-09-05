"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { detectPlatform } from "@/lib/platform";
import { createJob, getSettings, type PacingMode, type SettingsResponse } from "@/lib/api";

export function UrlForm() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [include, setInclude] = useState("posts,reels");
  const [videos, setVideos] = useState(true);
  const [anonymous, setAnonymous] = useState(false);
  const [albums, setAlbums] = useState(false);
  const [quickUpdate, setQuickUpdate] = useState(false);
  const [quickLimit, setQuickLimit] = useState(20);
  // "default" means send nothing and let the server's Settings decide.
  const [pacingMode, setPacingMode] = useState<PacingMode | "default">("default");
  const [pacingMin, setPacingMin] = useState(1);
  const [pacingMax, setPacingMax] = useState(30);
  // null, not 0: the job layer saying nothing must inherit the server's per-image delay rather
  // than switching it off. Only a number the operator actually typed is sent.
  const [pacingPerFile, setPacingPerFile] = useState<number | null>(null);
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
      if (platform === "facebook") {
        // Default is "photos" alone; albums re-walks pages `photos` already covered.
        if (albums) options["include"] = "photos,albums";
        if (quickUpdate) options["quick_update"] = quickLimit;
      }
      // Omitted entirely unless overridden, so the server's Settings stay in charge.
      if (pacingMode !== "default") {
        options["pacing"] = {
          mode: pacingMode,
          min: pacingMin,
          max: pacingMax,
          per_file: pacingPerFile,
        };
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
          <div className="collapse-title font-medium">Advanced options for this job</div>
          <div className="collapse-content flex flex-col gap-3 pt-2">
            {platform === "instagram" && (
              <>
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
              </>
            )}

            {platform === "facebook" && (
              <>
                <div className="flex flex-col gap-1">
                  <label className="label cursor-pointer justify-start gap-3">
                    <input
                      type="checkbox"
                      className="checkbox checkbox-sm"
                      checked={albums}
                      onChange={(e) => setAlbums(e.target.checked)}
                    />
                    <span className="label-text">Also walk albums</span>
                  </label>
                  <p className="pl-9 text-xs text-base-content/60">
                    Roughly doubles the time for very few extra files: an album re-fetches photo
                    pages the main walk already covered, and Facebook only knows a photo is already
                    downloaded <em>after</em> fetching its page.
                  </p>
                </div>
                <div className="flex flex-col gap-1">
                  <label className="label cursor-pointer justify-start gap-3">
                    <input
                      type="checkbox"
                      className="checkbox checkbox-sm"
                      checked={quickUpdate}
                      onChange={(e) => setQuickUpdate(e.target.checked)}
                    />
                    <span className="label-text">Quick update — stop after</span>
                    <input
                      type="number"
                      min={1}
                      className="input input-bordered input-xs w-20"
                      value={quickLimit}
                      disabled={!quickUpdate}
                      onChange={(e) => setQuickLimit(Number(e.target.value))}
                    />
                    <span className="label-text">already-downloaded files</span>
                  </label>
                  <p className="pl-9 text-xs text-base-content/60">
                    Facebook lists newest first, so a refresh reaches the new photos immediately and
                    then re-fetches every old page just to skip it. Leave this off if a previous run
                    was interrupted — it would stop before reaching the part you never got.
                  </p>
                </div>
              </>
            )}

            <div className="flex flex-col gap-2">
              <span className="label-text">Pacing for this job</span>
              <div className="flex flex-wrap items-center gap-3">
                <select
                  className="select select-bordered select-sm"
                  value={pacingMode}
                  onChange={(e) => setPacingMode(e.target.value as PacingMode | "default")}
                >
                  <option value="default">Server default</option>
                  <option value="adaptive">Adaptive</option>
                  <option value="fixed">Fixed</option>
                </select>
                {pacingMode !== "default" && (
                  <>
                    <label className="flex items-center gap-2">
                      <span className="label-text text-xs">
                        {pacingMode === "adaptive" ? "Floor (s)" : "Min (s)"}
                      </span>
                      <input
                        type="number"
                        min={0}
                        step="0.5"
                        className="input input-bordered input-sm w-20"
                        value={pacingMin}
                        onChange={(e) => setPacingMin(Number(e.target.value))}
                      />
                    </label>
                    <label className="flex items-center gap-2">
                      <span className="label-text text-xs">
                        {pacingMode === "adaptive" ? "Ceiling (s)" : "Max (s)"}
                      </span>
                      <input
                        type="number"
                        min={0}
                        step="0.5"
                        className="input input-bordered input-sm w-20"
                        value={pacingMax}
                        onChange={(e) => setPacingMax(Number(e.target.value))}
                      />
                    </label>
                    <label className="flex items-center gap-2">
                      <span className="label-text text-xs">Per image (s)</span>
                      <input
                        type="number"
                        min={0}
                        step="0.5"
                        placeholder="default"
                        className="input input-bordered input-sm w-24"
                        value={pacingPerFile ?? ""}
                        onChange={(e) =>
                          setPacingPerFile(e.target.value === "" ? null : Number(e.target.value))
                        }
                      />
                    </label>
                  </>
                )}
              </div>
              <p className="text-xs text-base-content/60">
                Adaptive starts at the floor and backs off only when the platform pushes back.
                Per image is a separate axis and applies in both modes; leave it blank to keep the
                server&apos;s value. Change the default for every job in{" "}
                <a className="link" href="/settings">
                  Settings
                </a>
                .
              </p>
            </div>
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
