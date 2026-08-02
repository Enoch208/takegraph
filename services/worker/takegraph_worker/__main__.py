"""Long-running TAKEGRAPH worker and reconciliation scheduler."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from takegraph_api.b2_reconciliation import B2UploadReconciler
from takegraph_api.db.session import dispose_engine, get_session_factory
from takegraph_infrastructure.b2 import B2Settings, B2Store

from takegraph_worker.gmi_gateway import GMICloudGateway, GMICloudSettings
from takegraph_worker.gmi_work import GMIWorkHandlers
from takegraph_worker.runtime import WorkerRuntime

logger = logging.getLogger("takegraph.worker")


def _load_local_env(path: Path = Path(".env")) -> None:
    """Load local development values without overriding deployment env vars."""
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _positive_int(name: str) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be configured as a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be configured as a positive integer")
    return value


def _log(event: str, **fields: object) -> None:
    logger.info(json.dumps({"event": event, **fields}, separators=(",", ":")))


async def run() -> None:
    _load_local_env()
    settings = B2Settings.from_env(dict(os.environ))
    store = B2Store(settings, preflight=True)
    factory = get_session_factory()
    environment = dict(os.environ)
    gmi_gateway = GMICloudGateway(GMICloudSettings.from_env(environment), settings)
    gmi_handlers = GMIWorkHandlers(factory, store, gmi_gateway, environment=environment)
    worker_id = os.environ.get("WORKER_ID", "")
    concurrency = _positive_int("WORKER_CONCURRENCY")
    runtime = WorkerRuntime(
        factory,
        store,
        owner=worker_id,
        lease_seconds=_positive_int("WORK_LEASE_SECONDS"),
        heartbeat_seconds=_positive_int("WORK_HEARTBEAT_SECONDS"),
        concurrency=concurrency,
        gmi_handlers=gmi_handlers,
    )
    reconciliation_interval = _positive_int("RECONCILIATION_INTERVAL_SECONDS")
    next_reconciliation = 0.0
    _log("worker.started", owner=worker_id, concurrency=concurrency)
    try:
        while True:
            monotonic_now = time.monotonic()
            if monotonic_now >= next_reconciliation:
                async with factory() as session:
                    reconciliation = await B2UploadReconciler(session, store).run_once()
                    await session.commit()
                _log(
                    "b2.reconciliation",
                    ran=reconciliation.ran,
                    scanned=reconciliation.scanned,
                    discovered=reconciliation.discovered,
                    queued=reconciliation.queued,
                )
                next_reconciliation = monotonic_now + reconciliation_interval

            batch = await runtime.run_once()
            if batch.claimed:
                _log(
                    "worker.batch",
                    claimed=batch.claimed,
                    completed=batch.completed,
                    failed=batch.failed,
                    lease_lost=batch.lease_lost,
                )
            else:
                await asyncio.sleep(0.5)
    finally:
        store.close()
        await dispose_engine()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        _log("worker.stopped", reason="interrupt")


if __name__ == "__main__":
    main()
