"""
BuraPay Gateway Benchmark — application entrypoint.

Mounted behind nginx at ``/api``. The React front end owns everything else.

Startup does three things: create the schema if Alembic has not (development only),
seed the gateway catalogue and the bootstrap admin, and start the read-only gateway
health probe loop.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.health import router as health_router
from app.api.v1.router import api_router
from app.api.webhooks import router as webhooks_router
from app.core.config import settings
from app.core.errors import (BenchmarkError, ErrorCategory, NotConfigured, NotSupported,
                             normalize)
from app.core.logging import configure_logging, get_logger, new_request_id, request_id_var
from app.db.base import Base
from app.db.session import dispose_engine, get_engine, get_sessionmaker
from app.services.bootstrap import bootstrap
from app.services.benchmark import BenchmarkRefused
from app.services.health import periodic_health_loop

configure_logging()
logger = get_logger(__name__)

DESCRIPTION = """
Benchmark and compare the technical performance of payment gateways.

**Test mode.** This deployment targets gateway sandbox environments only unless
`ALLOW_PRODUCTION_GATEWAYS` has been explicitly enabled.

**What the numbers mean.** *Gateway API time* is the sum of the timed merchant-server
calls to the gateway, measured with a monotonic clock from just before each request to
the moment its response body is fully read. *Total transaction time* additionally
includes redirects, 3DS and any time the customer spent on a hosted page. They answer
different questions and are never combined.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = get_engine()
    async with engine.begin() as connection:
        # Alembic owns the schema in any real deployment. This is the development and
        # test path, where running migrations before every pytest run is friction with
        # no benefit; create_all is a no-op once the tables exist.
        await connection.run_sync(Base.metadata.create_all)

    async with get_sessionmaker()() as session:
        await bootstrap(session)

    insecure = settings.insecure_defaults()
    if insecure:
        logger.warning("running with development secrets",
                       extra={"operation": "startup", "status": "insecure",
                              "variables": insecure})

    health_task = asyncio.create_task(periodic_health_loop())
    try:
        yield
    finally:
        health_task.cancel()
        try:
            await health_task
        except asyncio.CancelledError:
            pass
        await dispose_engine()


app = FastAPI(
    title=settings.app_name,
    description=DESCRIPTION,
    version=settings.app_version,
    lifespan=lifespan,
    # Served behind nginx at /api, so the documented paths line up with the public URL.
    root_path="/api",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Bind a request id, time the request, and set the baseline security headers."""
    request_id = request.headers.get("x-request-id") or new_request_id()
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    duration_ms = round((time.perf_counter() - started) * 1000, 3)

    response.headers["X-Request-ID"] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if settings.public_base_url.startswith("https://"):
        response.headers.setdefault("Strict-Transport-Security",
                                    "max-age=31536000; includeSubDomains")

    logger.info("request", extra={"method": request.method, "path": request.url.path,
                                  "http_status": response.status_code,
                                  "duration_ms": duration_ms,
                                  "status": "ok" if response.status_code < 400 else "error"})
    return response


# --------------------------------------------------------------------------- #
# Error handling — one normalized shape (specification section 37)
# --------------------------------------------------------------------------- #

@app.exception_handler(BenchmarkRefused)
async def handle_refused(request: Request, exc: BenchmarkRefused) -> JSONResponse:
    return JSONResponse(
        {"category": ErrorCategory.INVALID_REQUEST.value, "message": str(exc),
         "detail": None},
        status_code=status.HTTP_400_BAD_REQUEST)


@app.exception_handler(NotConfigured)
async def handle_not_configured(request: Request, exc: NotConfigured) -> JSONResponse:
    return JSONResponse(
        {"category": ErrorCategory.CONFIGURATION_ERROR.value, "message": str(exc)},
        status_code=status.HTTP_400_BAD_REQUEST)


@app.exception_handler(NotSupported)
async def handle_not_supported(request: Request, exc: NotSupported) -> JSONResponse:
    return JSONResponse(
        {"category": ErrorCategory.INVALID_REQUEST.value, "message": str(exc)},
        status_code=status.HTTP_400_BAD_REQUEST)


@app.exception_handler(BenchmarkError)
async def handle_gateway_error(request: Request, exc: BenchmarkError) -> JSONResponse:
    error = normalize(exc, gateway="", operation=request.url.path)
    return JSONResponse(error.to_dict(), status_code=status.HTTP_502_BAD_GATEWAY)


@app.exception_handler(RequestValidationError)
async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        {"category": ErrorCategory.INVALID_REQUEST.value,
         "message": "The request could not be validated.",
         "detail": {"errors": exc.errors()}},
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)


app.include_router(health_router)
app.include_router(webhooks_router)
app.include_router(api_router, prefix="/v1")


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/api/docs",
        "test_mode": not settings.allow_production_gateways,
    }
