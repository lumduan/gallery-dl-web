import { FileList } from "@/components/FileList";

export default function DownloadsPage() {
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-bold">Downloads</h1>
      <FileList />
    </div>
  );
}
