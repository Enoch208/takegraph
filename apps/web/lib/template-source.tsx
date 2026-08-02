import templateExport from "../../../t.json";
import { icons, type IconName } from "@/components/icon";

type TemplateExport = Array<{ code: string }>;

function readTemplateCode(): string {
  const code = (templateExport as TemplateExport)[0]?.code;
  if (!code) throw new Error("t.json does not contain a landing-page export");
  return code;
}

const templateCode = readTemplateCode();

const ICONS: Record<string, IconName> = {
  "arrow-right": "arrowRight",
  "arrow-up-right": "arrowUpRight",
  "chevron-right": "chevronRight",
  bell: "notification",
  globe: "globe",
  instagram: "instagram",
  play: "play",
  plus: "add",
  twitter: "twitter",
};

function classNameFrom(attributes: string): string {
  const className = attributes.match(/\bclass=["']([^"']*)["']/i)?.[1] ?? "";
  const withoutSourceRuntimeClasses = className
    .split(/\s+/)
    .filter((token) => token && token !== "lucide" && !token.startsWith("lucide-") && !token.startsWith("iconify"))
    .join(" ");
  return withoutSourceRuntimeClasses || "size-4";
}

function iconFor(attributes: string, contents: string): IconName {
  const lucideName = attributes.match(/\blucide-([a-z0-9-]+)/i)?.[1];
  if (lucideName && ICONS[lucideName]) return ICONS[lucideName];

  if (/data-icon=/i.test(attributes)) return "provider";
  if (/fill-current/i.test(attributes)) return "star";
  if (/translate-x|M5 12h14|M9 5l7 7|m9 18 6-6/i.test(attributes + contents)) {
    return "arrowRight";
  }
  if (/M7 17L17 7|arrow-up/i.test(attributes + contents)) return "arrowUpRight";
  if (/check|M20 6L9 17/i.test(attributes + contents)) return "verified";
  return "generate";
}

type HugeIconElement = readonly [string, Readonly<Record<string, string | number>>];

function escapeAttribute(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function attributeName(name: string): string {
  return name.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
}

function hugeIcon(name: IconName, className: string): string {
  const elements = icons[name] as unknown as readonly HugeIconElement[];
  const body = elements
    .map(([tag, attributes]) => {
      const serialized = Object.entries(attributes)
        .filter(([attribute]) => attribute !== "key")
        .map(([attribute, value]) => {
          const normalizedValue = attribute === "strokeWidth" ? "1.65" : String(value);
          return `${attributeName(attribute)}="${escapeAttribute(normalizedValue)}"`;
        })
        .join(" ");
      return `<${tag} ${serialized}></${tag}>`;
    })
    .join("");

  return `<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" color="currentColor" class="${escapeAttribute(className)}" aria-hidden="true">${body}</svg>`;
}

function replaceTemplateIcons(source: string): string {
  const withoutIconRuntimes = source
    .replace(/<script src="https:\/\/unpkg\.com\/lucide@latest"><\/script>\s*/i, "")
    .replace(/<script src="https:\/\/code\.iconify\.design\/3\/3\.1\.0\/iconify\.min\.js"><\/script>\s*/i, "")
    .replace(/<script>\s*lucide\.createIcons\(\);\s*<\/script>/gi, "")
    .replace(
      /<script[^>]*>\s*if \(typeof lucide !== ["']undefined["']\) \{\s*lucide\.createIcons\(\);\s*\}\s*<\/script>/gi,
      "",
    )
    .replace(/\s*lucide\.createIcons\(\);/gi, "");

  const withRenderedLucideTags = withoutIconRuntimes.replace(
    /<i\b([^>]*\bdata-lucide=["']([a-z0-9-]+)["'][^>]*)><\/i>/gi,
    (_tag, attributes: string, lucideName: string) =>
      hugeIcon(ICONS[lucideName] ?? "generate", classNameFrom(attributes)),
  );

  const withRenderedIconifySpans = withRenderedLucideTags.replace(
    /<span\b([^>]*\bdata-icon=["'][^"']+["'][^>]*)><\/span>/gi,
    (_tag, attributes: string) => hugeIcon("provider", classNameFrom(attributes)),
  );

  return withRenderedIconifySpans.replace(
    /<svg\b([^>]*)>([\s\S]*?)<\/svg>/gi,
    (svg, attributes: string, contents: string) => {
      const isIcon =
        /\bviewBox=["']0 0 24 24["']/i.test(attributes) ||
        /\blucide-|\bdata-icon=/i.test(attributes);

      if (!isIcon) return svg;
      return hugeIcon(iconFor(attributes, contents), classNameFrom(attributes));
    },
  );
}

/**
 * The JSON export is the visual source of truth. The only deliberate changes
 * are non-visual: remove Aura's cross-frame referral write and swap its icon
 * runtimes/markup for the project's HugeIcons registry. The Unicorn scene,
 * Tailwind runtime, typography, imagery, layout, animation and page scripts are
 * otherwise left byte-for-byte as supplied by the export.
 */
export function landingTemplateHtml(): string {
  const withoutReferralTracking = templateCode.replace(
    /<script>\s*try\{if\(window\.parent[\s\S]*?<\/script>\s*/i,
    "",
  );

  return replaceTemplateIcons(withoutReferralTracking);
}
