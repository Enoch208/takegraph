"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Hero backdrop — UnicornStudio scene over a static gradient.
 *
 * The scene is a third-party WebGL runtime fetched from a CDN, which is a
 * deliberate product decision rather than an oversight. Three consequences are
 * handled here rather than left to chance:
 *
 * 1. It can fail. The CDN can be blocked, WebGL can be unavailable, the project
 *    can be changed by its owner. The static gradient underneath always paints,
 *    so the hero never renders as a black rectangle.
 * 2. It must not block first paint. The script is injected after mount, so the
 *    headline and CTA are interactive regardless of how long the scene takes.
 * 3. §18.3 requires an equivalent static state under `prefers-reduced-motion`.
 *    The scene is simply not loaded in that case.
 *
 * Opacity is held low so the headline keeps its contrast (§18.14 wants 4.5:1 on
 * normal text, and a bright animated field behind large type erodes that fast).
 */

const SCENE_ID = "sajpUiTp7MIKdX6daDCu";

// Pinned. An unpinned CDN tag would let the bundle change under us between a
// rehearsal and the live demo.
const SCRIPT_SRC =
  "https://cdn.jsdelivr.net/gh/hiunicornstudio/unicornstudio.js@v1.4.29/dist/unicornStudio.umd.js";

declare global {
  interface Window {
    UnicornStudio?: { isInitialized?: boolean; init?: () => void };
  }
}

function webglAvailable(): boolean {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("webgl2") ?? canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

export function HeroBackdrop() {
  const mounted = useRef(false);
  const [sceneReady, setSceneReady] = useState(false);

  useEffect(() => {
    if (mounted.current) return;
    mounted.current = true;

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reducedMotion || !webglAvailable()) {
      // The static gradient below is the equivalent state, not a degraded one.
      return;
    }

    const start = () => {
      window.UnicornStudio?.init?.();
      if (window.UnicornStudio) window.UnicornStudio.isInitialized = true;
      setSceneReady(true);
    };

    if (window.UnicornStudio?.isInitialized) {
      setSceneReady(true);
      return;
    }

    const existing = document.querySelector<HTMLScriptElement>(`script[src="${SCRIPT_SRC}"]`);
    if (existing) {
      existing.addEventListener("load", start, { once: true });
      return;
    }

    window.UnicornStudio ??= { isInitialized: false };
    const script = document.createElement("script");
    script.src = SCRIPT_SRC;
    script.async = true;
    script.addEventListener("load", start, { once: true });
    // A failed load is not an error worth surfacing to the user — the fallback
    // is already on screen — but it must not leave a half-initialised global.
    script.addEventListener(
      "error",
      () => {
        window.UnicornStudio = { isInitialized: false };
      },
      { once: true },
    );
    document.head.appendChild(script);
  }, []);

  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
      {/* Always painted. Also the reduced-motion and no-WebGL state. */}
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(120% 85% at 50% 0%, #10161e 0%, #0a0e14 45%, var(--color-canvas) 100%)",
        }}
      />

      <div
        data-us-project={SCENE_ID}
        className={`absolute inset-0 transition-opacity duration-[1600ms] ease-out ${
          sceneReady ? "opacity-100" : "opacity-0"
        }`}
        style={{
          // The scene ships bright and crimson — it was authored for a template
          // whose accent is #ef233c. Rotating the hue lands it on TAKEGRAPH's
          // --signal (#FF6A35) so the backdrop agrees with the rest of the
          // palette, and the brightness cut keeps the headline's contrast.
          filter: "hue-rotate(22deg) brightness(0.4) saturate(0.7) contrast(1.05)",
          maskImage:
            "radial-gradient(125% 90% at 50% 34%, black 30%, rgba(0,0,0,0.55) 62%, transparent 88%)",
          WebkitMaskImage:
            "radial-gradient(125% 90% at 50% 34%, black 30%, rgba(0,0,0,0.55) 62%, transparent 88%)",
        }}
      />

      {/* Keeps the canvas from lifting the page's black point where type sits. */}
      <div
        className="absolute inset-0"
        style={{
          background:
            "linear-gradient(to bottom, transparent 0%, color-mix(in oklab, var(--color-canvas) 55%, transparent) 62%, var(--color-canvas) 100%)",
        }}
      />
    </div>
  );
}
