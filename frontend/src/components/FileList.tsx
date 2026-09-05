"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { downloadUrl, listFiles, type FileListResponse } from "@/lib/api";

function formatSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function FileList() {
  const [data, setData] = useState<FileListResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    listFiles()
      .then(setData)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : "failed to load"));
  }, []);

  if (err) return <div className="alert alert-error">{err}</div>;
  if (data === null) return <p className="text-base-content/60">Loading…</p>;
  const files = data.files;
  if (files.length === 0)
    return <p className="text-base-content/60">No downloads yet. Submit a URL to get started.</p>;

  return (
    <div className="overflow-x-auto">
      {data.truncated && (
        <p className="text-xs text-base-content/60 pb-2">
          Showing the {files.length.toLocaleString()} newest of{" "}
          {data.total.toLocaleString()} files. Open a profile from{" "}
          <Link className="link" href="/profiles">
            Profiles
          </Link>{" "}
          to browse the rest.
        </p>
      )}
      <table className="table table-zebra">
        <thead>
          <tr>
            <th>File</th>
            <th>Platform</th>
            <th>Size</th>
            <th>Modified</th>
            <th aria-label="download" />
          </tr>
        </thead>
        <tbody>
          {files.map((f) => (
            <tr key={f.path}>
              <td className="font-mono text-xs break-all">{f.path}</td>
              <td>
                <span className="badge badge-sm">{f.platform}</span>
              </td>
              <td className="text-xs whitespace-nowrap">{formatSize(f.size)}</td>
              <td className="text-xs whitespace-nowrap">
                {new Date(f.mtime * 1000).toLocaleString()}
              </td>
              <td>
                <a className="btn btn-xs btn-ghost" href={downloadUrl(f.path)}>
                  ⬇
                </a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
