"""Pre-render every poster in a build so a demo never waits on B2.

The first request for an asset costs a B2 read and a downscale — around five
seconds for a large original. Running this before a demo moves that cost off the
critical path, so the storyboard paints from local cache and the run costs no B2
transactions at all.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def _post(base: str, path: str, payload: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - local base
        return json.load(response)


def _get(base: str, path: str, token: str) -> dict[str, object]:
    request = urllib.request.Request(
        f"{base}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - local base
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    token = str(_post(args.base, "/api/v1/demo/session", {})["access_token"])
    project = _get(args.base, "/api/v1/demo/project", token)
    build_id = project["build_id"]
    graph = _get(args.base, f"/api/v1/builds/{build_id}/graph", token)

    asset_ids: list[str] = []
    for node in graph["nodes"]:  # type: ignore[index]
        for asset in node["selected_assets"]:
            mime = str(asset["mime_type"])
            if mime.startswith(("image/", "video/")):
                asset_ids.append(str(asset["id"]))

    unique = list(dict.fromkeys(asset_ids))
    print(f"build {build_id}: {len(unique)} posters to warm")

    warmed = 0
    failed = 0
    started = time.monotonic()
    for asset_id in unique:
        request = urllib.request.Request(
            f"{args.base}/api/v1/assets/{asset_id}/thumbnail",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310
                body = response.read()
            warmed += 1
            print(f"  ok   {asset_id[:8]}  {len(body) / 1024:6.1f} KB")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            failed += 1
            print(f"  FAIL {asset_id[:8]}  {exc}", file=sys.stderr)

    elapsed = time.monotonic() - started
    print(f"\nwarmed {warmed}/{len(unique)} in {elapsed:.1f}s, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
