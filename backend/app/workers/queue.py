"""
Redis-backed job queue (§1).

Used for work that genuinely must not sit inside a request: a benchmark run of
N transactions takes minutes and would hold an HTTP connection open for all of
it. Everything a caller needs for the immediate answer -- the run's id and its
accepted/refused status -- is decided synchronously before the job is enqueued,
so the response is complete and the queue only carries the slow part.

Nothing in the payment path uses this. A payment is the immediate answer to the
request that made it, and moving it to a worker would mean the caller could not
be told whether it succeeded.
"""

from __future__ import annotations

import functools
import json
from typing import Any, Mapping

from redis import Redis
from rq import Queue

from app.config import get_settings
from app.security.crypto import decrypt, encrypt

QUEUE_NAME = "burapay"


@functools.lru_cache(maxsize=1)
def get_redis() -> Redis:
    return Redis.from_url(get_settings().REDIS_URL)


@functools.lru_cache(maxsize=1)
def get_queue() -> Queue:
    # A benchmark run is bounded by BENCHMARK_MAX_TRANSACTIONS_PER_RUN, but each
    # transaction can take as long as HTTP_TIMEOUT_SECONDS, so the job timeout
    # has to allow for the worst case rather than a typical one.
    settings = get_settings()
    worst_case = int(
        settings.BENCHMARK_MAX_TRANSACTIONS_PER_RUN * settings.HTTP_TIMEOUT_SECONDS * 6
    )
    return Queue(QUEUE_NAME, connection=get_redis(), default_timeout=max(600, worst_case))


def enqueue(function: Any, *args: Any, description: str | None = None, **kwargs: Any) -> Any:
    """
    Enqueue a job with an explicit description.

    The description is not cosmetic. RQ builds one by ``repr``-ing the call when
    none is given, and writes it to the worker log on every state change -- so a
    job whose arguments contain card data would print that card data into the
    application log. Passing a description that names the job instead of its
    arguments is what stops that (§0.8).
    """
    return get_queue().enqueue(
        function, *args, description=description or getattr(function, "__name__", "job"),
        **kwargs,
    )


def seal(payload: Mapping[str, Any]) -> str:
    """
    Encrypt a job payload that carries cardholder data.

    A queued job is persisted by Redis, so plaintext card data in a job
    argument would be card data written to disk -- which §0.8 forbids as
    plainly as writing it to a log table. The payload is sealed with the same
    ENCRYPTION_KEY used for stored credentials and opened only inside the
    worker, for the duration of the job.
    """
    return encrypt(json.dumps(dict(payload)))


def unseal(token: str) -> dict[str, Any]:
    return json.loads(decrypt(token))


def queue_available() -> bool:
    """
    True when Redis is reachable.

    Checked before enqueuing so the API can say "the queue is down" instead of
    accepting a run that will never execute.
    """
    try:
        return bool(get_redis().ping())
    except Exception:
        return False
