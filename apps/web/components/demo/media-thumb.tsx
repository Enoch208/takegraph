"use client";

import { useEffect, useState } from "react";
import { fetchAssetAccess, type SelectedAsset } from "@/lib/api";
import { Icon } from "@/components/icon";

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

  useEffect(() => {
    let cancelled = false;
    setUrl(null);
    setError(null);
    if (!asset) {
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
  }, [asset, token]);

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
    return (
      <div
        className={`flex items-center justify-center border border-border bg-elevated text-faint ${className}`}
      >
        <Icon name="running" className="size-4 animate-pulse" />
      </div>
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
    return <img src={url} alt="" className={`object-cover ${className}`} />;
  }

  return (
    <div
      className={`flex items-center justify-center border border-border bg-elevated font-mono text-[10px] text-muted ${className}`}
    >
      {asset.media_kind}
    </div>
  );
}
