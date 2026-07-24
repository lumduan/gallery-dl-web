// Theme preference — the single source of truth for the storage key and the accepted values.
// Deliberately NOT a client module: the server layout imports THEME_INIT_SCRIPT, the client
// toggle imports the helpers, and both stay in agreement about the key and the value set.

export type ThemeMode = "system" | "light" | "dark";

export const THEME_STORAGE_KEY = "theme";

/**
 * "system" is the ABSENCE of `data-theme`, not a value.
 *
 * DaisyUI emits its `--prefersdark` theme as
 * `@media (prefers-color-scheme: dark) { :root:not([data-theme]) { … } }`, so leaving the
 * attribute off lets the OS drive the palette (and `color-scheme`) in pure CSS — including an OS
 * change while the page is open, with no `matchMedia` listener. Setting it makes the `:not()`
 * guard stand down, which is what pins an explicit light/dark choice.
 */
export function applyTheme(mode: ThemeMode): void {
  const root = document.documentElement;
  if (mode === "light" || mode === "dark") root.setAttribute("data-theme", mode);
  else root.removeAttribute("data-theme");
}

/** Anything unrecognized — or storage being unavailable — means "system". */
export function readTheme(): ThemeMode {
  try {
    const v = localStorage.getItem(THEME_STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    // localStorage throws in private mode / when storage is blocked.
  }
  return "system";
}

export function storeTheme(mode: ThemeMode): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, mode);
  } catch {
    // Non-fatal: the choice just won't survive a reload.
  }
}

/**
 * Runs synchronously in `<head>`, before the browser paints, so an explicit light/dark choice
 * never flashes the other palette. See Next's own guide for this technique:
 * node_modules/next/dist/docs/01-app/02-guides/preventing-flash-before-hydration.md § Themes.
 *
 * It only ever ADDS the attribute — the server renders `<html>` without one (= system), which the
 * stylesheet already resolves correctly on its own.
 */
export const THEME_INIT_SCRIPT =
  `(function(){try{var t=localStorage.getItem(${JSON.stringify(THEME_STORAGE_KEY)});` +
  `if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t)}catch(e){}})()`;
