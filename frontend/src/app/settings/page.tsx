import { CookieForm } from "@/components/CookieForm";
import { ThemeSetting } from "@/components/ThemeSetting";

export default function SettingsPage() {
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-bold">Settings</h1>
      <p className="text-sm text-base-content/70">
        Credentials are stored only in the backend&apos;s <code>/data/cookies.json</code> (mode 0600,
        gitignored). They are never returned over the API — only whether each is set.
      </p>

      <div className="alert alert-info flex flex-col items-start gap-2 py-3">
        <div className="font-semibold">📷 Easiest: use the browser extension</div>
        <div className="text-sm">
          Load the <code>extension/</code> folder unpacked (Chrome / Edge / Brave), set this
          server&apos;s URL in the popup, then click <b>Send Instagram session</b> /{" "}
          <b>Send Facebook cookies</b> while logged in. It reads your own cookies and sends them here
          — one click, refreshable. See{" "}
          <a
            className="link"
            href="https://github.com/lumduan/gallery-dl-web/blob/main/extension/README.md"
            target="_blank"
            rel="noreferrer"
          >
            extension/README.md
          </a>
          .
        </div>
      </div>

      <div className="divider text-xs text-base-content/50">or paste manually</div>

      <CookieForm />

      <div className="divider text-xs text-base-content/50">appearance</div>

      <ThemeSetting />
    </div>
  );
}
