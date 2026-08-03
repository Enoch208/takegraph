"""TAKEGRAPH FastAPI control plane.

Route handlers hold transport logic only; business rules live in the domain
package (PRD §7.1). Health endpoints follow §11.2: `/health/live` makes no
dependency calls, `/health/ready` summarises dependencies without leaking secrets.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from takegraph_domain.enums import ApiErrorCode, CapabilityState
from takegraph_domain.errors import DomainError, FeatureNotConfiguredError
from takegraph_infrastructure.b2 import B2Settings, B2Store

from takegraph_api.b2_webhooks import router as b2_webhook_router
from takegraph_api.builds import router as builds_router
from takegraph_api.changes import router as changes_router
from takegraph_api.db.session import session_scope
from takegraph_api.demo import router as demo_router
from takegraph_api.projection import DemoProof, load_demo_proof
from takegraph_api.projects import router as projects_router
from takegraph_api.release_routes import router as releases_router
from takegraph_api.uploads import router as uploads_router

API_PREFIX = "/api/v1"

app = FastAPI(
    title="TAKEGRAPH API",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.include_router(b2_webhook_router)
app.include_router(builds_router)
app.include_router(demo_router)
app.include_router(changes_router)
app.include_router(projects_router)
app.include_router(releases_router)
app.include_router(uploads_router)


@app.middleware("http")
async def correlation_id(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """§11.1: accept or generate X-Request-ID and echo it back, so an error shown
    in the UI can be traced to a log line (§21.1)."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    """§9.8: one typed error envelope, mapped once at the boundary. No stack
    traces, SQL, secrets, provider bodies or internal paths cross this line."""
    status = {
        ApiErrorCode.NOT_FOUND: 404,
        ApiErrorCode.FORBIDDEN: 403,
        ApiErrorCode.UNAUTHENTICATED: 401,
        ApiErrorCode.VERSION_CONFLICT: 409,
        ApiErrorCode.UPLOAD_INCOMPLETE: 409,
        ApiErrorCode.IMPACT_PLAN_STALE: 409,
        ApiErrorCode.BUILD_NOT_RUNNABLE: 409,
        ApiErrorCode.IDEMPOTENCY_CONFLICT: 409,
        ApiErrorCode.RATE_LIMITED: 429,
        ApiErrorCode.B2_SIGNATURE_INVALID: 401,
        ApiErrorCode.FEATURE_NOT_CONFIGURED: 503,
    }.get(exc.error_code, 400 if exc.error_code != ApiErrorCode.INTERNAL_ERROR else 500)

    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": str(exc.error_code),
                "message": exc.message,
                "request_id": getattr(request.state, "request_id", None),
                "details": exc.details,
            }
        },
    )


class Liveness(BaseModel):
    status: str


@app.get("/health/live", response_model=Liveness, tags=["health"])
async def health_live() -> Liveness:
    """§11.2: process liveness only. Deliberately makes no dependency calls."""
    return Liveness(status="ok")


class Readiness(BaseModel):
    status: str
    database: str
    redis: str
    storage: str
    providers: str


@app.get("/health/ready", response_model=Readiness, tags=["health"])
async def health_ready() -> Readiness:
    """Dependency readiness, summarised without secrets (§11.2, §19.6).

    Reports NOT_CONFIGURED rather than inventing a healthy answer. §24.5 forbids
    a missing credential from silently becoming a working fixture, so an
    unconfigured dependency is visible here and the capability that needs it stays
    switched off.
    """

    def configured(var: str) -> str:
        return (
            str(CapabilityState.ENABLED)
            if os.environ.get(var)
            else str(CapabilityState.NOT_CONFIGURED)
        )

    database = configured("DATABASE_URL")
    return Readiness(
        # The impact engine needs no dependency, so the API is useful before any
        # credential exists — but it must not claim to be fully ready.
        status="degraded" if database != str(CapabilityState.ENABLED) else "ok",
        database=database,
        redis=configured("REDIS_URL"),
        storage=configured("B2_KEY_ID"),
        providers=configured("GMI_API_KEY"),
    )


@app.get(f"{API_PREFIX}/demo/proof", response_model=DemoProof, tags=["demo"])
async def demo_proof() -> DemoProof:
    """Numbers for the landing page's proof strip, computed by the real impact
    engine (§4.4 forbids hard-coding them in React).

    The response carries `source` and `verified_build` so the UI states plainly
    whether these came from a projection over the seed template or from a real
    build's persisted events.
    """
    # Storage is optional here. If B2 is unconfigured the proof still returns —
    # it simply carries no poster, which is honest rather than fatal (§20.6:
    # one unavailable dependency must not take down the whole surface).
    store: B2Store | None = None
    try:
        store = B2Store(B2Settings.from_env(dict(os.environ)))
    except FeatureNotConfiguredError:
        store = None

    sign: Callable[[str], str] | None = None
    if store is not None:
        # Bound to a local so the closure cannot capture a None `store`, and so
        # the type checker can prove it.
        bound = store
        ttl = int(os.environ.get("B2_SIGNED_URL_TTL_SECONDS", "900"))

        def sign_key(key: str) -> str:
            return bound.presign_get(key, ttl_seconds=ttl)

        sign = sign_key

    try:
        async with session_scope() as session:
            return await load_demo_proof(session, sign=sign)
    finally:
        if store is not None:
            store.close()
