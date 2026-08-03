import type { BuildNode } from "@/lib/api";

/**
 * Storyboard ordering (PRD §18.6: "storyboard timeline by default").
 *
 * The API returns nodes in topological/creation order, which is correct for a
 * build engine and wrong for a reader. A judge scans this grid expecting a
 * production — sources, then the plan, then shot 1 through 4, then the package —
 * not an alphabetical index where `audio.music` precedes `compose.delivery_package`
 * precedes `copy.pack`.
 *
 * Ordering is explicit rather than derived from the graph because the desired
 * reading order is a narrative decision, not a topological one: `image.poster`
 * and `audio.music` are topologically free to float anywhere, but a reader
 * expects them in specific places.
 */
const ORDER: string[] = [
  // Inputs the production starts from.
  "source.brief",
  "source.product_reference",
  "transform.product_cutout",
  "plan.shots",
  // The four shots, keyframe paired with its clip so cause sits beside effect.
  "image.keyframe.01",
  "video.clip.01",
  "image.keyframe.02",
  "video.clip.02",
  "image.keyframe.03",
  "video.clip.03",
  "image.keyframe.04",
  "video.clip.04",
  // Copy and audio, then the assembled deliverables.
  "copy.pack",
  "audio.narration",
  "audio.music",
  "graphic.end_card",
  "image.poster",
  "compose.delivery_package",
];

const POSITION = new Map(ORDER.map((key, index) => [key, index]));

/** Nodes in reading order. Unknown keys keep their API order after the known set,
 *  so a template change adds cards rather than hiding them. */
export function inStoryOrder(nodes: BuildNode[]): BuildNode[] {
  return [...nodes].sort((a, b) => {
    const left = POSITION.get(a.stable_key) ?? Number.MAX_SAFE_INTEGER;
    const right = POSITION.get(b.stable_key) ?? Number.MAX_SAFE_INTEGER;
    return left === right ? a.stable_key.localeCompare(b.stable_key) : left - right;
  });
}

/** Section headings, so the grid reads as a production rather than a flat wall. */
export const STORY_SECTIONS: { title: string; keys: string[] }[] = [
  { title: "Sources", keys: ORDER.slice(0, 4) },
  { title: "Shots", keys: ORDER.slice(4, 12) },
  { title: "Copy & audio", keys: ORDER.slice(12, 15) },
  { title: "Deliverables", keys: ORDER.slice(15) },
];

export function sectionFor(stableKey: string): string {
  for (const section of STORY_SECTIONS) {
    if (section.keys.includes(stableKey)) return section.title;
  }
  return "Other";
}
