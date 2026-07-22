import { ProfileGrid } from "@/components/ProfileGrid";

export default function ProfilesPage() {
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-bold">Profiles</h1>
      <p className="text-sm text-base-content/70">
        Each downloaded Instagram / Facebook profile is indexed here, organized by username.
      </p>
      <ProfileGrid />
    </div>
  );
}
