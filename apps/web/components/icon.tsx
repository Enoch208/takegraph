import type { SVGProps } from "react";
import { HugeiconsIcon } from "@hugeicons/react";
import {
  Alert02Icon,
  ArrowRight01Icon,
  CheckmarkCircle02Icon,
  CpuIcon,
  Database02Icon,
  FileVerifiedIcon,
  GitBranchIcon,
  Layers01Icon,
  LockIcon,
  PlayIcon,
  RefreshIcon,
  SecurityCheckIcon,
  Share08Icon,
  Shield01Icon,
  SparklesIcon,
  Timer02Icon,
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
const icons = {
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
  play: PlayIcon,
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
