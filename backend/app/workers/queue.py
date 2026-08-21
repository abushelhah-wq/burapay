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
from typing import Any

from redis import Redis
from rq import Queue

from app.config import get_settings

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


def enqueue(function: Any, *args: Any, **kwargs: Any) -> Any:
    return get_queue().enqueue(function, *args, **kwargs)


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
