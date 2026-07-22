"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { deleteProfile, getProfile, profileZipUrl, type ProfileMetadata } from "@/lib/api";

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

export function ProfileGallery({ platform, name }: { platform: string; name: string }) {
  const router = useRouter();
  const [meta, setMeta] = useState<ProfileMetadata | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getProfile(platform, name)
      .then(setMeta)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : "failed to load"));
  }, [platform, name]);

  async function onDelete() {
    if (
      !confirm(
        `Delete profile "${name}" and all its files? This frees storage and cannot be undone.`,
      )
    )
      return;
    setBusy(true);
    try {
      await deleteProfile(platform, name);
      router.push("/profiles");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "delete failed");
      setBusy(false);
    }
  }

  if (err) return <div className="alert alert-error">{err}</div>;
  if (meta === null) return <p className="text-base-content/60">Loading…</p>;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <h1 className="text-2xl font-bold truncate flex items-center gap-2">
          {name} <span className="badge badge-sm">{platform}</span>
        </h1>
        <div className="flex gap-2">
          <a className="btn btn-primary btn-sm" href={profileZipUrl(platform, name)}>
            ⬇ Download .zip
          </a>
          <button className="btn btn-error btn-sm" onClick={onDelete} disabled={busy}>
            {busy ? "Deleting…" : "Delete profile"}
          </button>
        </div>
      </div>

      <div className="stats stats-vertical sm:stats-horizontal shadow bg-base-200">
        <div className="stat">
          <div className="stat-title">Images</div>
          <div className="stat-value text-success">{meta.image_count}</div>
        </div>
        <div className="stat">
          <div className="stat-title">Videos</div>
          <div className="stat-value">{meta.video_count}</div>
        </div>
        <div className="stat">
          <div className="stat-title">Size</div>
          <div className="stat-value text-lg">{formatBytes(meta.total_bytes)}</div>
        </div>
      </div>

      {meta.images.length === 0 && meta.videos.length === 0 ? (
        <p className="text-base-content/60">No media yet.</p>
      ) : (
        <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-2">
          {meta.images.map((f) => (
            <a
              key={f.path}
              href={f.file_url}
              target="_blank"
              rel="noreferrer"
              className="aspect-square overflow-hidden bg-base-200 rounded"
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={f.thumb_url}
                alt={f.filename}
                loading="lazy"
                className="w-full h-full object-cover hover:opacity-80 transition-opacity"
              />
            </a>
          ))}
          {meta.videos.map((f) => (
            <a
              key={f.path}
              href={f.file_url}
              target="_blank"
              rel="noreferrer"
              className="aspect-square overflow-hidden bg-base-300 rounded flex items-center justify-center text-3xl"
            >
              🎬
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
