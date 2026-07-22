"use client";

import { useParams } from "next/navigation";

import { ProfileGallery } from "@/components/ProfileGallery";

export default function ProfilePage() {
  const params = useParams<{ platform: string; name: string }>();
  if (!params.platform || !params.name)
    return <p className="text-base-content/60">Loading…</p>;
  return <ProfileGallery platform={params.platform} name={params.name} />;
}
