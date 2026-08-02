import Image from "next/image";
import Link from "next/link";
import { Icon } from "@/components/icon";

const LINKS = [
  { href: "#product", label: "Product" },
  { href: "#how-it-works", label: "How It Works" },
  { href: "#architecture", label: "Architecture" },
  { href: "#proof", label: "Proof" },
] as const;

export function Nav() {
  return (
    <div className="fixed inset-x-0 top-0 z-50 flex justify-center px-4 pt-6">
      <nav
        aria-label="Primary"
        className="rise flex w-full max-w-5xl items-center justify-between gap-8 border border-white/10 bg-black/60 py-2 pl-3 pr-2 shadow-2xl shadow-black/50 backdrop-blur-lg"
        style={{ animationDelay: "0.9s" }}
      >
        <Link href="/" className="flex shrink-0 items-center gap-2.5">
          <Image
            src="/brand/mark.png"
            alt=""
            width={512}
            height={512}
            priority
            className="size-6 w-auto"
          />
          <span className="font-display text-[15px] font-semibold tracking-tight">TAKEGRAPH</span>
        </Link>

        <div className="hidden items-center gap-7 md:flex">
          {LINKS.map((link) => (
            <a
              key={link.href}
              href={link.href}
              className="group relative py-1 text-sm font-medium text-muted transition-colors hover:text-ink"
            >
              {link.label}
              <span className="absolute -bottom-2.5 left-1/2 h-px w-0 -translate-x-1/2 bg-signal transition-all duration-300 ease-out group-hover:w-full" />
            </a>
          ))}
        </div>

        <Link
          href="/demo"
          className="group relative inline-flex h-[38px] shrink-0 items-center justify-center gap-2 overflow-hidden bg-white/5 px-5 transition-transform active:scale-95"
        >
          <span className="absolute inset-0 bg-border transition-opacity duration-300 group-hover:opacity-0" />
          <span className="absolute inset-px bg-canvas" />
          <span
            className="absolute inset-0 opacity-0 transition-opacity duration-500 group-hover:opacity-100"
            style={{
              background:
                "radial-gradient(50% 50% at 50% 100%, color-mix(in oklab, var(--color-signal) 25%, transparent) 0%, transparent 100%)",
            }}
          />
          <span className="relative z-10 flex items-center gap-2 text-[11px] font-medium uppercase tracking-wide">
            Open live build
            <Icon
              name="arrowRight"
              className="size-3.5 transition-transform duration-300 group-hover:translate-x-0.5"
            />
          </span>
        </Link>
      </nav>
    </div>
  );
}
