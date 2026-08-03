import Link from "next/link";
import { Icon, type IconName } from "@/components/icon";

const ITEMS: { href: string; name: IconName; label: string; active?: boolean }[] = [
  { href: "/demo", name: "demo", label: "Demo workspace", active: true },
  { href: "/demo#release", name: "release", label: "Release proof" },
  { href: "/system/status", name: "system", label: "System status" },
];

export function IconRail({ releaseHref }: { releaseHref?: string }) {
  return (
    <aside className="flex w-14 shrink-0 flex-col items-center border-r border-border bg-surface py-4">
      <Link
        href="/"
        className="mb-6 flex size-9 items-center justify-center border border-border bg-elevated font-display text-sm font-medium tracking-tighter text-signal"
        aria-label="TAKEGRAPH home"
      >
        TG
      </Link>
      <nav className="flex flex-1 flex-col items-center gap-2" aria-label="Workspace">
        {ITEMS.map((item) => {
          const href = item.name === "release" && releaseHref ? releaseHref : item.href;
          return (
            <Link
              key={item.name}
              href={href}
              aria-label={item.label}
              className={`flex size-10 items-center justify-center border transition-colors ${
                item.active
                  ? "border-signal/50 bg-signal/10 text-signal"
                  : "border-transparent text-muted hover:border-border hover:text-ink"
              }`}
            >
              <Icon name={item.name} className="size-5" />
            </Link>
          );
        })}
      </nav>
      <Link
        href="/"
        className="flex size-10 items-center justify-center text-muted hover:text-ink"
        aria-label="Back to landing"
      >
        <Icon name="arrowUpRight" className="size-4" />
      </Link>
    </aside>
  );
}
