import { CookieForm } from "@/components/CookieForm";

export default function SettingsPage() {
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-bold">Settings</h1>
      <p className="text-sm text-base-content/70">
        Credentials are stored only in the backend&apos;s <code>/data/cookies.json</code> (mode 0600,
        gitignored). They are never returned over the API — only whether each is set.
      </p>
      <CookieForm />
    </div>
  );
}
