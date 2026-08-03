"""Long-running TAKEGRAPH worker and reconciliation scheduler."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from takegraph_api.b2_reconciliation import B2UploadReconciler
from takegraph_api.db.session import dispose_engine, get_engine, get_session_factory
from takegraph_infrastructure.b2 import B2Settings, B2Store

from takegraph_worker.gmi_gateway import GMICloudGateway, GMICloudSettings
from takegraph_worker.gmi_work import GMIWorkHandlers
from takegraph_worker.runtime import WorkerRuntime

logger = logging.getLogger("takegraph.worker")

#: Infrastructure faults between this process and Postgres — a dropped socket, a
#: database restart, a pre-ping that timed out on a half-open connection. These
#: say nothing about whether the work is doable, and §8.3.10's leases guarantee
#: that anything claimed becomes claimable again once its lease expires, so
#: nothing is lost by backing off and reconnecting.
#:
#: Deliberately narrow. A bug in a handler, a bad configuration or a failed
#: invariant is not in this tuple and still takes the process down.
_TRANSIENT_DB_ERRORS = (OperationalError, InterfaceError, DBAPIError, OSError)

#: Give up rather than spin forever. If the database has not come back after this
#: many consecutive attempts (~2 minutes at the backoff below), the problem is not
#: transient and a supervisor restarting the process is more useful than a worker
#: that looks alive while doing nothing.
_MAX_CONSECUTIVE_DB_FAILURES = 10
_BACKOFF_CEILING_SECONDS = 30.0


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
    consecutive_db_failures = 0
    _log("worker.started", owner=worker_id, concurrency=concurrency)
    try:
        while True:
            try:
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
            except _TRANSIENT_DB_ERRORS as exc:
                consecutive_db_failures += 1
                # Loudly, with the exception — a worker that swallows this and
                # keeps looping is indistinguishable from one that is idle.
                logger.exception("takegraph.worker database error")
                _log(
                    "worker.db_error",
                    error_type=type(exc).__name__,
                    error=str(exc)[:200],
                    consecutive=consecutive_db_failures,
                )
                if consecutive_db_failures >= _MAX_CONSECUTIVE_DB_FAILURES:
                    _log("worker.giving_up", consecutive=consecutive_db_failures)
                    raise
                # The pool is the likeliest thing holding dead sockets, so drop
                # it. dispose() installs a fresh pool and leaves the engine (and
                # therefore the session factory captured above) usable.
                await get_engine().dispose()
                await asyncio.sleep(min(2.0**consecutive_db_failures, _BACKOFF_CEILING_SECONDS))
                continue

            consecutive_db_failures = 0
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
