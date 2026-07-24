import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { Navbar } from "@/components/Navbar";
import { THEME_INIT_SCRIPT } from "@/lib/theme";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "gallery-dl-web",
  description: "Download Instagram & Facebook images via gallery-dl.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <head>
        {/* Applies a stored light/dark choice before the first paint. No `data-theme` is rendered
            here on purpose: its absence IS "system", which the stylesheet resolves from
            prefers-color-scheme. `suppressHydrationWarning` above covers the attribute this adds. */}
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="min-h-full flex flex-col bg-base-200">
        <Navbar />
        <main className="flex-1 container mx-auto px-4 py-8 max-w-3xl">{children}</main>
        <footer className="footer footer-center p-4 text-base-content/60 text-xs">
          <aside>
            <p>
              gallery-dl-web · wraps{" "}
              <a
                className="link"
                href="https://github.com/mikf/gallery-dl"
                target="_blank"
                rel="noreferrer"
              >
                gallery-dl
              </a>
            </p>
          </aside>
        </footer>
      </body>
    </html>
  );
}
