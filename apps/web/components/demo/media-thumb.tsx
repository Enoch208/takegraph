"use client";

import { useEffect, useRef, useState } from "react";
import type { SelectedAsset } from "@/lib/api";
import { Icon } from "@/components/icon";

/**
 * A storyboard tile's picture.
 *
 * This used to point the browser at the full-resolution original in B2 — a 1.6 MB
 * render, or an MP4 — to fill a box a couple of hundred pixels wide, for all
 * eighteen nodes at once. It was slow (the storyboard sat empty for about fourteen
 * seconds), it spent a B2 Class B transaction and the object's egress per tile on
 * every page view, and when that daily cap was reached B2 answered 403 with an XML
 * error document. A browser asked to render XML as an image draws a broken-image
 * glyph, so the dashboard looked broken while behaving correctly.
 *
 * Now it asks the API for a cached, downscaled poster. Same-origin, so no
 * presigned-URL expiry and no cross-origin blocking; content-addressed and
 * immutable, so a reload comes from the browser cache; and the API only reaches
 * B2 the first time any viewer asks for a given asset.
 *
 * Two further guards, because a demo runs unattended: nothing is requested until
 * the tile approaches the viewport, and a poster that fails renders a stated
 * fallback rather than a broken glyph.
 */
const NEAR_VIEWPORT = "600px";

function hasPoster(asset: SelectedAsset): boolean {
  return asset.mime_type.startsWith("image/") || asset.mime_type.startsWith("video/");
}

export function MediaThumb({
  asset,
  token,
  className = "",
}: {
  asset: SelectedAsset | null | undefined;
  token: string;
  className?: string;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [visible, setVisible] = useState(false);
  const holder = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const node = holder.current;
    if (!node || visible) {
      return;
    }
    // Without IntersectionObserver — older browsers, and jsdom under test — load
    // immediately. Degrading to "slower" is correct; degrading to "no media" is not.
    if (typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: NEAR_VIEWPORT },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [visible]);

  useEffect(() => {
    if (!asset || !visible || !hasPoster(asset)) {
      return;
    }
    let objectUrl: string | null = null;
    let cancelled = false;
    setError(null);

    void (async () => {
      try {
        // Fetched rather than assigned to <img src> because the route is
        // authorised and an image element cannot carry a bearer token. The
        // response still comes from the browser's HTTP cache on reload — the URL
        // and its immutable Cache-Control do that work.
        const response = await fetch(`/api/v1/assets/${asset.id}/thumbnail`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) {
          throw new Error(`poster returned ${response.status}`);
        }
        const blob = await response.blob();
        if (cancelled) {
          return;
        }
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      } catch (cause) {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : "poster unavailable");
        }
      }
    })();

    return () => {
      cancelled = true;
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
      }
    };
  }, [asset, token, visible]);

  if (!asset) {
    return (
      <div
        className={`flex items-center justify-center border border-dashed border-hairline bg-elevated text-faint ${className}`}
      >
        <Icon name="layers" className="size-5" />
      </div>
    );
  }

  // Audio and JSON have no picture. Naming what the node holds is more useful
  // than an empty frame, and it costs no request at all.
  if (!hasPoster(asset)) {
    const audio = asset.mime_type.startsWith("audio/");
    return (
      <div
        className={`flex flex-col items-center justify-center gap-1.5 bg-elevated text-faint ${className}`}
      >
        <Icon name={audio ? "play" : "verified"} className="size-5" />
        <span className="font-mono text-[10px] uppercase tracking-wider">
          {audio ? "Audio" : "Document"}
        </span>
      </div>
    );
  }

  if (error) {
    return (
      <div
        className={`flex flex-col items-center justify-center gap-1.5 border border-dashed border-hairline bg-elevated px-2 text-center text-faint ${className}`}
        title={error}
      >
        <Icon name="failed" className="size-4" />
        <span className="font-mono text-[10px] uppercase tracking-wider">No preview</span>
      </div>
    );
  }

  if (!src) {
    // The observer needs a mounted node to watch, so the placeholder carries the ref.
    return (
      <div
        ref={holder}
        className={`skeleton bg-elevated ${className}`}
        role="img"
        aria-label="Loading preview"
      />
    );
  }

  return (
    <img
      src={src}
      alt=""
      decoding="async"
      onError={() => setError("poster failed to decode")}
      className={`object-cover ${className}`}
    />
  );
}
