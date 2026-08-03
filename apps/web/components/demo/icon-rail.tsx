"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Icon, type IconName } from "@/components/icon";

const ITEMS: { href: string; name: IconName; label: string; match: string }[] = [
  { href: "/demo", name: "demo", label: "Build workspace", match: "/demo" },
  { href: "/system/status", name: "system", label: "System status", match: "/system" },
];

/**
 * The reference's left rail: logo at the top, icon navigation, exit pinned to the
 * bottom. Narrow enough to cost almost no horizontal budget, and the active item
 * marked by a filled chip rather than a text label.
 *
 * What a static mockup does not have to solve: an icon-only control has no
 * accessible name, and an active state drawn only in colour is invisible to a
 * portion of the audience. Every item carries `aria-label` and `title`, the
 * active one carries `aria-current`, and it is marked by a bar as well as a tint
 * (§18.14). Targets are 44px.
 */
export function IconRail({ releaseHref }: { releaseHref?: string }) {
  const pathname = usePathname();
  const items = releaseHref
    ? [
        ...ITEMS,
        {
          href: releaseHref,
          name: "release" as IconName,
          label: "Release proof",
          match: "/releases",
        },
      ]
    : ITEMS;

  return (
    <aside className="flex w-[68px] shrink-0 flex-col items-center gap-1 border-r border-hairline bg-surface py-4">
      <Link
        href="/"
        className="hit mb-4 flex size-11 items-center justify-center rounded-[var(--radius-control)] bg-signal/12 font-display text-[13px] font-semibold tracking-tighter text-signal transition-colors hover:bg-signal/20"
        aria-label="TAKEGRAPH home"
      >
        TG
      </Link>

      <nav className="flex flex-1 flex-col items-center gap-1.5" aria-label="Workspace sections">
        {items.map((item) => {
          const active = pathname === item.href || pathname.startsWith(`${item.match}/`);
          return (
            <Link
              key={item.name}
              href={item.href}
              aria-label={item.label}
              aria-current={active ? "page" : undefined}
              title={item.label}
              className={`hit relative flex size-11 items-center justify-center rounded-[var(--radius-control)] transition-colors ${
                active ? "bg-signal/12 text-signal" : "text-faint hover:bg-elevated hover:text-ink"
              }`}
            >
              {active ? (
                <span className="absolute -left-[14px] h-5 w-[3px] rounded-r-full bg-signal" />
              ) : null}
              <Icon name={item.name} className="size-[19px]" />
            </Link>
          );
        })}
      </nav>

      <Link
        href="/"
        className="hit flex size-11 items-center justify-center rounded-[var(--radius-control)] text-faint transition-colors hover:bg-elevated hover:text-ink"
        aria-label="Leave the workspace"
        title="Leave the workspace"
      >
        <Icon name="arrowUpRight" className="size-[18px]" />
      </Link>
    </aside>
  );
}
