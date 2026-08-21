"""
FastAPI application.

Serves a JSON REST API only. The frontend is a separate container talking to
this over HTTP (§1) -- there are no templates here and no HTML is rendered for
the main application.

Startup runs the bootstrap (reference data, first admin, credential seeding).
Alembic migrations are run by the container entrypoint before this process
starts, so the schema is always current by the time anything serves traffic.
"""

from __future__ import annotations

import contextlib
from typing import AsyncIterator

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import MissingConfiguration, get_settings
from app.db import dispose_engine, session_scope
from app.gateways.errors import (
    CredentialsMissing, DocumentationRequiredError, FeatureDisabled, GatewayError,
    InvalidTransactionState, MerchantCapabilityError, ProductionGatewayBlocked,
    UnsupportedOperationError,
)
from app.logging_setup import configure_logging, get_logger
from app.routers import analytics, auth, gateways, health, payments, settings as settings_router
from app.routers import transactions as transactions_router
from app.routers import webhooks
from app.security.crypto import is_configured as encryption_configured
from app.services.bootstrap import run_bootstrap

logger = get_logger(__name__)

#: HTTP status for each typed gateway failure. Chosen so the frontend can react
#: without string-matching a message: 501 means "this build cannot do it", 409
#: means "not in this state", 403 means "a gate refused you".
_ERROR_STATUS: dict[type[GatewayError], int] = {
    UnsupportedOperationError: status.HTTP_501_NOT_IMPLEMENTED,
    DocumentationRequiredError: status.HTTP_501_NOT_IMPLEMENTED,
    CredentialsMissing: status.HTTP_409_CONFLICT,
    ProductionGatewayBlocked: status.HTTP_403_FORBIDDEN,
    FeatureDisabled: status.HTTP_403_FORBIDDEN,
    InvalidTransactionState: status.HTTP_409_CONFLICT,
    MerchantCapabilityError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    logger.info(
        "starting burapay api",
        extra={
            "public_base_url": settings.public_base_url,
            "public_base_is_https": settings.public_base_is_https,
            "allow_direct_card_entry": settings.ALLOW_DIRECT_CARD_ENTRY,
            "allow_production_gateways": settings.ALLOW_PRODUCTION_GATEWAYS,
            "http_timeout_seconds": settings.HTTP_TIMEOUT_SECONDS,
            "measurement_location": settings.MEASUREMENT_LOCATION or None,
        },
    )

    # Warn rather than refuse: the API must still come up so an operator can
    # reach the Settings page and see what is wrong. Every code path that needs
    # one of these raises its own clear error at the point of use.
    if not settings.APP_SECRET_KEY:
        logger.error("APP_SECRET_KEY is not set; nobody will be able to sign in")
    if not encryption_configured():
        logger.error(
            "ENCRYPTION_KEY is not usable; gateway credentials cannot be "
            "stored or read"
        )
    if not settings.public_base_url:
        logger.error(
            "PUBLIC_BASE_URL is not set; hosted checkout and callbacks cannot work"
        )
    elif not settings.public_base_is_https:
        logger.warning(
            "PUBLIC_BASE_URL is not https; gateway sandboxes reject non-HTTPS "
            "return and callback URLs, so hosted checkout will fail",
            extra={"public_base_url": settings.public_base_url},
        )

    try:
        async with session_scope() as session:
            await run_bootstrap(session)
    except Exception:  # noqa: BLE001
        # A failed bootstrap must not prevent /health from answering -- that is
        # how an operator finds out the database is unreachable.
        logger.exception("bootstrap failed")

    yield

    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="BuraPay",
        description=(
            "Payment gateway performance benchmarking. Measures how many API "
            "round trips each gateway needs to complete the same operation, and "
            "how long each one takes, against real sandbox APIs."
        ),
        version="2.0.0",
        lifespan=lifespan,
    )

    origins = settings.cors_origins
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(gateways.router)
    app.include_router(payments.router)
    app.include_router(transactions_router.router)
    app.include_router(analytics.router)
    app.include_router(settings_router.router)
    app.include_router(webhooks.router)

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
        """
        Turn a typed gateway failure into a structured response.

        ``doc_url`` is included for DocumentationRequiredError so the UI can
        link straight to the page that needs reading, rather than showing a
        dead end.
        """
        payload: dict[str, object] = {"code": exc.code, "message": exc.message}
        if isinstance(exc, DocumentationRequiredError):
            payload["doc_url"] = exc.doc_url
            payload["missing"] = exc.missing
        if isinstance(exc, UnsupportedOperationError):
            payload["operation"] = exc.operation
        if isinstance(exc, CredentialsMissing):
            payload["missing_credentials"] = exc.keys

        return JSONResponse(
            status_code=_ERROR_STATUS.get(type(exc), status.HTTP_502_BAD_GATEWAY),
            content={"error": payload},
        )

    @app.exception_handler(MissingConfiguration)
    async def missing_config_handler(
        request: Request, exc: MissingConfiguration
    ) -> JSONResponse:
        logger.error("missing configuration", extra={"detail": str(exc)})
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": {"code": "missing_configuration", "message": str(exc)}},
        )

    return app


app = create_app()
