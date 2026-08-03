import type { SVGProps } from "react";
import { HugeiconsIcon } from "@hugeicons/react";
import {
  Add01Icon,
  Alert02Icon,
  ArrowRight01Icon,
  ArrowUpRight01Icon,
  Cancel01Icon,
  CheckmarkCircle02Icon,
  ChevronRightIcon,
  CpuIcon,
  DashboardSquare01Icon,
  Database02Icon,
  FileVerifiedIcon,
  GitBranchIcon,
  GlobeIcon,
  InstagramIcon,
  Layers01Icon,
  LockIcon,
  Menu01Icon,
  Notification02Icon,
  PlayIcon,
  RefreshIcon,
  Search01Icon,
  SecurityCheckIcon,
  Share08Icon,
  Shield01Icon,
  SparklesIcon,
  StarIcon,
  SystemUpdate01Icon,
  Timer02Icon,
  TwitterIcon,
  Video01Icon,
  ViewIcon,
} from "@hugeicons/core-free-icons";

/**
 * The single icon registry.
 *
 * Every icon in the product resolves through this map. Call sites use a semantic
 * name — `<Icon name="reused" />` — never a glyph name, so swapping a glyph later
 * is one line here rather than a search across the codebase.
 *
 * There is no second icon library, no pasted raw SVG, and no emoji standing in
 * for an icon. If a glyph is missing, find the HugeIcons equivalent and add it
 * here; do not install another icon package for one shape.
 */
export const icons = {
  // Node and build state — these mirror the domain enums, not the visual shape.
  reused: CheckmarkCircle02Icon,
  verified: FileVerifiedIcon,
  rebuild: RefreshIcon,
  review: Alert02Icon,
  running: Timer02Icon,

  // Capability index
  generate: SparklesIcon,
  validate: SecurityCheckIcon,
  recover: Shield01Icon,
  release: LockIcon,

  // Structure and architecture
  graph: Share08Icon,
  lineage: GitBranchIcon,
  storage: Database02Icon,
  provider: CpuIcon,
  layers: Layers01Icon,

  // Navigation and controls
  arrowRight: ArrowRight01Icon,
  arrowUpRight: ArrowUpRight01Icon,
  chevronRight: ChevronRightIcon,
  add: Add01Icon,
  notification: Notification02Icon,
  play: PlayIcon,
  search: Search01Icon,
  close: Cancel01Icon,
  menu: Menu01Icon,
  demo: DashboardSquare01Icon,
  video: Video01Icon,
  view: ViewIcon,
  system: SystemUpdate01Icon,
  failed: Alert02Icon,

  // Template social and decorative glyphs
  instagram: InstagramIcon,
  twitter: TwitterIcon,
  globe: GlobeIcon,
  star: StarIcon,
} as const;

export type IconName = keyof typeof icons;

type IconProps = { name: IconName; className?: string } & Omit<
  SVGProps<SVGSVGElement>,
  "ref" | "size" | "strokeWidth"
>;

/**
 * `size="1em"` plus a `size-*` class means an icon scales with the text it sits
 * beside instead of being hardcoded per call site. `color="currentColor"` means
 * every theme and hover state works without touching this file. One strokeWidth
 * constant keeps the set visually coherent — mismatched stroke weights are the
 * clearest tell of a UI assembled from several sources.
 *
 * Icons are decorative and hidden from assistive tech. When an icon is a
 * control's only content, the control itself carries an `aria-label`.
 */
export function Icon({ name, className = "size-4", ...props }: IconProps) {
  return (
    <HugeiconsIcon
      icon={icons[name]}
      size="1em"
      strokeWidth={1.65}
      color="currentColor"
      className={className}
      aria-hidden="true"
      {...props}
    />
  );
}
