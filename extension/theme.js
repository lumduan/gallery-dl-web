// Theme for the popup: System (follows the OS) / Light / Dark.
//
// Mirrors the web app's mechanism deliberately — "system" is the ABSENCE of `data-theme` on
// <html>, so the `prefers-color-scheme` rules in popup.html drive the palette and `color-scheme`
// with no listener, and an explicit choice just sets the attribute.
//
// Two things here are load-bearing and look wrong at a glance:
//
// 1. This is a separate FILE loaded from <head>, not an inline <script>. Manifest V3's extension
//    CSP is `script-src 'self'`, so an inline script would silently never run.
// 2. It stores the preference in localStorage, not `chrome.storage.local` like the server URL.
//    chrome.storage is async — awaiting it means the popup paints light and then flips, which in a
//    320px popup is the whole window flashing. localStorage is synchronous, so reading it here in
//    <head> applies the theme before the first paint.

const THEME_KEY = "theme";
const MODES = ["system", "light", "dark"];

function readTheme() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    if (MODES.includes(v)) return v;
  } catch {
    // localStorage can be unavailable if the user has blocked storage.
  }
  return "system";
}

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === "light" || mode === "dark") root.setAttribute("data-theme", mode);
  else root.removeAttribute("data-theme");
}

// Runs during <head> parsing, before the body exists — hence before anything is painted.
applyTheme(readTheme());

document.addEventListener("DOMContentLoaded", () => {
  const buttons = [...document.querySelectorAll(".theme-mode")];

  // Which button is *shown* as selected is done in CSS off `data-theme`, so it is already correct
  // by now. `aria-pressed` can't be expressed that way, so set it here — it is not visual, so
  // doing it after paint costs nothing.
  const syncPressed = (mode) => {
    for (const b of buttons) b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
  };
  syncPressed(readTheme());

  for (const b of buttons) {
    b.addEventListener("click", () => {
      const mode = b.dataset.mode;
      try {
        localStorage.setItem(THEME_KEY, mode);
      } catch {
        // Non-fatal: the choice just won't survive the popup closing.
      }
      applyTheme(mode);
      syncPressed(mode);
    });
  }
});
