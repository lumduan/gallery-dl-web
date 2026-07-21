"use client";

import { useEffect, useState } from "react";
import { getSettings, updateCookies, type SettingsResponse } from "@/lib/api";

export function CookieForm() {
  const [ig, setIg] = useState("");
  const [fb, setFb] = useState("");
  const [status, setStatus] = useState<SettingsResponse | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getSettings().then(setStatus).catch(() => {});
  }, []);

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (f) setFb(await f.text());
  }

  async function save() {
    setBusy(true);
    setMsg(null);
    try {
      const body: { ig_sessionid?: string; fb_cookies_text?: string } = {};
      if (ig) body.ig_sessionid = ig;
      if (fb) body.fb_cookies_text = fb;
      const s = await updateCookies(body);
      setStatus(s);
      setMsg({ kind: "ok", text: "Saved. Cookie values are never shown back." });
      setIg("");
      setFb("");
    } catch (err) {
      setMsg({
        kind: "err",
        text: err instanceof Error ? err.message : "Save failed",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="card bg-base-100 shadow border border-base-300">
        <div className="card-body gap-3">
          <div className="flex items-center justify-between">
            <h2 className="card-title">Instagram</h2>
            {status?.has_ig && <span className="badge badge-success badge-sm">configured</span>}
          </div>
          <p className="text-xs text-base-content/60">
            Paste your <code>sessionid</code> cookie. gallery-dl disables password login, so a cookie
            is required. In a desktop browser: DevTools → Application → Cookies → instagram.com →
            copy <code>sessionid</code>.
          </p>
          <input
            type="password"
            className="input input-bordered w-full"
            placeholder="sessionid"
            value={ig}
            onChange={(e) => setIg(e.target.value)}
            autoComplete="off"
          />
        </div>
      </div>

      <div className="card bg-base-100 shadow border border-base-300">
        <div className="card-body gap-3">
          <div className="flex items-center justify-between">
            <h2 className="card-title">Facebook</h2>
            {status?.has_fb && <span className="badge badge-success badge-sm">configured</span>}
          </div>
          <p className="text-xs text-base-content/60">
            Paste a Netscape <code>cookies.txt</code> (export with a browser extension) or upload the
            file. <strong>These cookies grant full account access — use a dedicated account.</strong>
          </p>
          <textarea
            className="textarea textarea-bordered w-full h-40 font-mono text-xs"
            placeholder={"# Netscape HTTP Cookie File\n.facebook.com\tTRUE\t/\tTRUE\t1900000000\tc_user\t…"}
            value={fb}
            onChange={(e) => setFb(e.target.value)}
          />
          <input
            type="file"
            accept=".txt,text/plain"
            className="file-input file-input-bordered file-input-sm"
            onChange={onFile}
          />
        </div>
      </div>

      {msg && (
        <div className={`alert ${msg.kind === "ok" ? "alert-success" : "alert-error"} py-2 text-sm`}>
          {msg.text}
        </div>
      )}
      <button className="btn btn-primary self-start" onClick={save} disabled={busy}>
        {busy ? "Saving…" : "Save cookies"}
      </button>
    </div>
  );
}
