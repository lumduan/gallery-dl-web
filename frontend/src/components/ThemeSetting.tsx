"use client";

import { applyTheme, storeTheme, THEME_OPTIONS, type ThemeMode } from "@/lib/theme";

/**
 * The Settings-page presentation of the same preference the navbar menu sets — an explicit,
 * always-visible list with a line of explanation per mode.
 *
 * Like the navbar menu it holds **no React state**: the `.theme-option-*` markers from
 * `THEME_OPTIONS` let the CSS in globals.css show the ✓ and embolden the selected row, so the two
 * controls agree automatically no matter which one was used. It deliberately does not register a
 * cross-tab `storage` listener — the navbar owns that one and is mounted on every page, including
 * this one.
 */
export function ThemeSetting() {
  function choose(mode: ThemeMode) {
    storeTheme(mode);
    applyTheme(mode);
  }

  return (
    <div className="card bg-base-100 shadow border border-base-300">
      <div className="card-body gap-3">
        <h2 className="card-title">Appearance</h2>
        <p className="text-xs text-base-content/60">
          Stored in this browser only — nothing is sent to the server, so another browser can differ.
        </p>
        <div className="flex flex-col gap-1">
          {THEME_OPTIONS.map((o) => (
            <button
              key={o.mode}
              type="button"
              className={`${o.marker} theme-choice flex items-center gap-3 w-full text-left rounded-lg px-3 py-2 hover:bg-base-200`}
              onClick={() => choose(o.mode)}
            >
              <span aria-hidden="true" className="text-lg">
                {o.icon}
              </span>
              <span className="flex-1">
                <span className="theme-label block text-sm">{o.label}</span>
                <span className="block text-xs text-base-content/60">{o.description}</span>
              </span>
              <span className="theme-check text-lg" role="img" aria-label="selected">
                ✓
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
