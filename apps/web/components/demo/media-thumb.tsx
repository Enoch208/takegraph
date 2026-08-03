"use client";

import { useEffect, useRef, useState } from "react";
import { fetchAssetAccess, type SelectedAsset } from "@/lib/api";
import { Icon } from "@/components/icon";

/**
 * Media is fetched only once the tile is near the viewport.
 *
 * Eighteen nodes, each holding a full-resolution original — a 2048×2048 render
 * or an MP4 — displayed in a tile a couple of hundred pixels wide. Requesting all
 * of them at first paint took roughly fourteen seconds before the storyboard
 * stopped looking empty, which is the first thing anyone opening the demo sees.
 *
 * It is also not free. Every one of those is a B2 Class B transaction plus its
 * egress, on a bucket with a daily transaction cap, charged again on every page
 * view. Gating on visibility means a viewer pays for the tiles they actually look
 * at. `rootMargin` starts the fetch before the tile is on screen so scrolling
 * still feels instant.
 */
const NEAR_VIEWPORT = "600px";

export function MediaThumb({
  asset,
  token,
  className = "",
}: {
  asset: SelectedAsset | null | undefined;
  token: string;
  className?: string;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [visible, setVisible] = useState(false);
  const holder = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const node = holder.current;
    if (!node || visible) {
      return;
    }
    // Without IntersectionObserver — older browsers, and jsdom under test — load
    // immediately. Degrading to "slower" is correct; degrading to "no media" is
    // not.
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
    let cancelled = false;
    setUrl(null);
    setError(null);
    if (!asset || !visible) {
      return;
    }
    void (async () => {
      const result = await fetchAssetAccess(asset.access_path, token);
      if (cancelled) {
        return;
      }
      if (!result.ok) {
        setError(result.error);
        return;
      }
      setUrl(result.data.access_url);
    })();
    return () => {
      cancelled = true;
    };
  }, [asset, token, visible]);

  if (!asset) {
    return (
      <div
        className={`flex items-center justify-center border border-dashed border-border bg-elevated text-faint ${className}`}
      >
        <Icon name="layers" className="size-5" />
      </div>
    );
  }

  if (error) {
    return (
      <div
        className={`flex items-center justify-center border border-dashed border-danger/40 bg-elevated px-2 text-center font-mono text-[10px] text-danger ${className}`}
      >
        media unavailable
      </div>
    );
  }

  if (!url) {
    // The observer needs a mounted node to watch, so the placeholder carries the
    // ref rather than the loaded media.
    return (
      <div
        ref={holder}
        className={`skeleton bg-elevated ${className}`}
        role="img"
        aria-label="Loading media"
      />
    );
  }

  if (asset.media_kind === "VIDEO" || asset.mime_type.startsWith("video/")) {
    return (
      <video
        // The #t media fragment seeks to a frame just past the start, so the
        // element paints a poster instead of a black rectangle. `preload
        //="metadata"` alone loads dimensions and duration but never decodes a
        // frame, which is why every clip tile rendered empty.
        src={`${url}#t=0.1`}
        className={`object-cover ${className}`}
        muted
        playsInline
        preload="metadata"
        onMouseEnter={(event) => void event.currentTarget.play().catch(() => undefined)}
        onMouseLeave={(event) => {
          event.currentTarget.pause();
          event.currentTarget.currentTime = 0.1;
        }}
      />
    );
  }

  if (asset.media_kind === "IMAGE" || asset.mime_type.startsWith("image/")) {
    return (
      <img
        src={url}
        alt=""
        // Decoding off the main thread keeps a 2048px original from stalling the
        // storyboard's paint while several tiles arrive at once.
        decoding="async"
        className={`object-cover ${className}`}
      />
    );
  }

  return (
    <div
      className={`flex items-center justify-center border border-border bg-elevated font-mono text-[10px] text-muted ${className}`}
    >
      {asset.media_kind}
    </div>
  );
}
