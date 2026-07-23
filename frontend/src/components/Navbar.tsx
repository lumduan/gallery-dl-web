"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  { href: "/", label: "Download" },
  { href: "/queue", label: "Queue" },
  { href: "/profiles", label: "Profiles" },
  { href: "/downloads", label: "Downloads" },
  { href: "/settings", label: "Settings" },
];

export function Navbar() {
  const pathname = usePathname();
  return (
    <div className="navbar bg-base-100 border-b border-base-300 sticky top-0 z-10">
      <div className="container mx-auto max-w-3xl flex-1">
        <div className="flex-1">
          <Link href="/" className="btn btn-ghost text-lg font-bold">
            📸 gallery-dl-web
          </Link>
        </div>
        <div className="flex-none">
          <ul className="menu menu-horizontal gap-1">
            {links.map((l) => (
              <li key={l.href}>
                <Link
                  href={l.href}
                  className={pathname === l.href ? "active font-semibold" : ""}
                >
                  {l.label}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
