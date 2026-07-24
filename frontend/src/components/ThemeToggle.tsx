"use client";

import { useEffect } from "react";

import { applyTheme, readTheme, storeTheme, THEME_STORAGE_KEY, type ThemeMode } from "@/lib/theme";

const OPTIONS: { mode: ThemeMode; icon: string; label: string; marker: string }[] = [
  { mode: "system", icon: "🖥️", label: "System", marker: "theme-option-system" },
  { mode: "light", icon: "☀️", label: "Light", marker: "theme-option-light" },
  { mode: "dark", icon: "🌙", label: "Dark", marker: "theme-option-dark" },
];

/**
 * Theme menu: System (follows the OS) / Light / Dark, persisted per browser.
 *
 * Holds **no React state**. `data-theme` on `<html>` is the whole state, and all three modes are
 * distinguishable from CSS — `:root:not([data-theme])` is system, `[data-theme=light|dark]` are the
 * explicit choices — so the `.theme-*` rules in globals.css drive both the trigger icon and the ✓.
 * That means there is nothing to hydrate, nothing to mismatch, and no flash: the markup is already
 * correct when the `<head>` script in layout.tsx runs, and "system" keeps tracking a live OS change
 * with no listener.
 *
 * The trigger shows the RESOLVED appearance (sun or moon), the menu shows the SELECTED mode — two
 * different things on purpose.
 */
export function ThemeToggle() {
  useEffect(() => {
    // Another tab changed the preference — mirror it onto this document. Purely a DOM update; the
    // icon and ✓ follow the attribute on their own.
    const sync = (e: StorageEvent) => {
      if (e.key === THEME_STORAGE_KEY) applyTheme(readTheme());
    };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);

  function choose(mode: ThemeMode, trigger: HTMLElement) {
    storeTheme(mode);
    applyTheme(mode);
    trigger.blur(); // DaisyUI dropdowns stay open on :focus-within, so blurring is what closes it.
  }

  return (
    <div className="dropdown dropdown-end">
      <div
        tabIndex={0}
        role="button"
        aria-label="Theme"
        title="Theme"
        className="btn btn-ghost btn-sm text-base"
      >
        <span className="theme-icon-light" aria-hidden="true">
          ☀️
        </span>
        <span className="theme-icon-dark" aria-hidden="true">
          🌙
        </span>
      </div>
      <ul
        tabIndex={0}
        className="dropdown-content menu w-40 p-2 gap-1 bg-base-100 border border-base-300 rounded-box shadow"
      >
        {OPTIONS.map((o) => (
          <li key={o.mode}>
            <button
              type="button"
              className={o.marker}
              onClick={(e) => choose(o.mode, e.currentTarget)}
            >
              <span aria-hidden="true">{o.icon}</span>
              {o.label}
              <span className="theme-check justify-self-end" role="img" aria-label="selected">
                ✓
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
