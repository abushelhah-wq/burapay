"""The v1 API router (specification section 29)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (auth, benchmarks, comparison, gateways, logs, reports,
                        settings, transactions)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(gateways.router)
api_router.include_router(transactions.router)
api_router.include_router(benchmarks.router)
api_router.include_router(comparison.router)
api_router.include_router(reports.router)
api_router.include_router(logs.router)
api_router.include_router(settings.router)
