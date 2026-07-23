import { QueueList } from "@/components/QueueList";

export default function QueuePage() {
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-bold">Queue</h1>
      <p className="text-sm text-base-content/70">
        Everything downloading, waiting, or paused. Pausing frees a slot so a waiting profile starts
        straight away; stopping keeps the files already downloaded.
      </p>
      <QueueList />
    </div>
  );
}
