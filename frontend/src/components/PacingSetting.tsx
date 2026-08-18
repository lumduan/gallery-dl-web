"use client";

import { useEffect, useState } from "react";
import {
  getSettings,
  updatePacing,
  type Pacing,
  type PacingStatus,
  type SettingsResponse,
} from "@/lib/api";

type Platform = "instagram" | "facebook";

const PLATFORMS: { id: Platform; label: string; hint: string }[] = [
  {
    id: "facebook",
    label: "Facebook",
    hint: "Facebook fetches a full page for every single photo, so the delay is paid once per image — this is the one worth tuning.",
  },
  {
    id: "instagram",
    label: "Instagram",
    hint: "Instagram returns ~30 posts per request, so the delay spreads across them and costs far less. Its floor is deliberately more cautious.",
  },
];

/**
 * Server-side, unlike the theme card next to it: pacing is what the *worker* does, so it has to
 * be the same for every browser, and it has to survive a restart. Applies to the next job — a
 * running one keeps the pacing it started with.
 */
export function PacingSetting() {
  const [status, setStatus] = useState<SettingsResponse["pacing"] | null>(null);
  const [draft, setDraft] = useState<Partial<Record<Platform, Pacing>>>({});
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState<Platform | null>(null);

  useEffect(() => {
    getSettings()
      .then((s) => setStatus(s.pacing))
      .catch(() => {});
  }, []);

  function valueFor(platform: Platform): Pacing | null {
    const live = status?.[platform];
    return draft[platform] ?? (live ? { mode: live.mode, min: live.min, max: live.max } : null);
  }

  function edit(platform: Platform, patch: Partial<Pacing>) {
    const current = valueFor(platform);
    if (current) setDraft({ ...draft, [platform]: { ...current, ...patch } });
  }

  async function save(platform: Platform, pacing: Pacing | null) {
    setBusy(platform);
    setMsg(null);
    try {
      const s = await updatePacing(platform, pacing);
      setStatus(s.pacing);
      setDraft({ ...draft, [platform]: undefined });
      setMsg({ kind: "ok", text: "Saved — applies to the next job you start." });
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : "Save failed" });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="card bg-base-100 shadow border border-base-300">
      <div className="card-body gap-4">
        <h2 className="card-title">Download pacing</h2>
        <p className="text-xs text-base-content/60">
          Both platforms rate-limit scraping. In <strong>Adaptive</strong> mode a run starts at the
          floor and only slows down when the platform pushes back, then speeds back up — and the
          floor rises on its own as a run gets long, since that is when a block actually happens.
          Choose <strong>Fixed</strong> to sleep a random amount in the range on every request
          instead.
        </p>

        {PLATFORMS.map(({ id, label, hint }) => {
          const value = valueFor(id);
          const live: PacingStatus | undefined = status?.[id];
          if (!value) return null;
          const dirty = draft[id] !== undefined;
          return (
            <div key={id} className="rounded-box border border-base-300 p-3 flex flex-col gap-3">
              <div className="flex items-center gap-2">
                <span className="font-medium">{label}</span>
                {live?.overridden && <span className="badge badge-sm">custom</span>}
              </div>
              <p className="text-xs text-base-content/60">{hint}</p>

              <div className="flex flex-wrap items-end gap-3">
                <label className="form-control">
                  <div className="label py-1">
                    <span className="label-text text-xs">Mode</span>
                  </div>
                  <select
                    className="select select-bordered select-sm"
                    value={value.mode}
                    onChange={(e) => edit(id, { mode: e.target.value as Pacing["mode"] })}
                  >
                    <option value="adaptive">Adaptive</option>
                    <option value="fixed">Fixed</option>
                  </select>
                </label>
                <label className="form-control">
                  <div className="label py-1">
                    <span className="label-text text-xs">
                      {value.mode === "adaptive" ? "Floor (s)" : "Min (s)"}
                    </span>
                  </div>
                  <input
                    type="number"
                    min={0}
                    step="0.5"
                    className="input input-bordered input-sm w-24"
                    value={value.min}
                    onChange={(e) => edit(id, { min: Number(e.target.value) })}
                  />
                </label>
                <label className="form-control">
                  <div className="label py-1">
                    <span className="label-text text-xs">
                      {value.mode === "adaptive" ? "Ceiling (s)" : "Max (s)"}
                    </span>
                  </div>
                  <input
                    type="number"
                    min={0}
                    step="0.5"
                    className="input input-bordered input-sm w-24"
                    value={value.max}
                    onChange={(e) => edit(id, { max: Number(e.target.value) })}
                  />
                </label>
                <button
                  className="btn btn-primary btn-sm"
                  disabled={busy === id || !dirty}
                  onClick={() => save(id, value)}
                >
                  {busy === id ? "Saving…" : "Save"}
                </button>
                {live?.overridden && (
                  <button
                    className="btn btn-ghost btn-sm"
                    disabled={busy === id}
                    onClick={() => save(id, null)}
                  >
                    Reset to default
                  </button>
                )}
              </div>
            </div>
          );
        })}

        {msg && (
          <div
            className={`alert ${msg.kind === "ok" ? "alert-success" : "alert-error"} py-2 text-sm`}
          >
            {msg.text}
          </div>
        )}
        <p className="text-xs text-base-content/60">
          Stored on the server, so it is the same in every browser. If you do get blocked, raise the
          floor and wait — retrying immediately extends the block.
        </p>
      </div>
    </div>
  );
}
