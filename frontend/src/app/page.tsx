import { UrlForm } from "@/components/UrlForm";

export default function Home() {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h1 className="text-3xl font-bold">Download Instagram &amp; Facebook images</h1>
        <p className="text-base-content/70">
          Paste a post, reel, or profile URL. Downloads run through{" "}
          <a
            className="link"
            href="https://github.com/mikf/gallery-dl"
            target="_blank"
            rel="noreferrer"
          >
            gallery-dl
          </a>{" "}
          with live progress.
        </p>
      </div>
      <UrlForm />
    </div>
  );
}
