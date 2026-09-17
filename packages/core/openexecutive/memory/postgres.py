"""Pooled Postgres connection for chat history.

Chat history (``conversations`` + ``messages``) moves to Postgres when
``DATABASE_URL`` is set — on Fly.io that variable is injected by
``fly mpg attach`` / ``fly postgres attach``. When it is unset the session
store keeps using the SQLite tables in ``episodic_memory.db`` (local dev,
tests, single-box installs), so nothing here is imported unless the URL is
present.

One process-wide ``psycopg_pool.ConnectionPool`` is created lazily on first
use and reused for the life of the process. The API runs as a single
instance (see ``docs/deployment.md``), so a small pool is plenty and stays far
inside a Managed Postgres connection budget. Size it with
``DATABASE_POOL_MAX_SIZE`` (default 4).

The pool is a plain sync pool on purpose: every session-store call site is
sync today (routes, integrations, the SSE persist step), matching how the
SQLite path already behaves.
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection
    from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

ENV_DATABASE_URL = "DATABASE_URL"
ENV_POOL_MAX_SIZE = "DATABASE_POOL_MAX_SIZE"
_DEFAULT_POOL_MAX_SIZE = 4
_POOL_MIN_SIZE = 1
# Seconds a caller waits for a free connection before giving up. Bounded so a
# saturated pool surfaces as an error instead of hanging an HTTP request.
_POOL_WAIT_TIMEOUT_S = 10.0
# Idle connections above min_size are closed after this many seconds so a
# quiet instance does not pin connections it no longer needs.
_POOL_MAX_IDLE_S = 300.0

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def database_url() -> str | None:
    """The configured ``DATABASE_URL`` or ``None`` when chat history is on SQLite."""
    raw = os.environ.get(ENV_DATABASE_URL, "")
    raw = raw.strip()
    return raw or None


def is_configured() -> bool:
    return database_url() is not None


def _pool_max_size() -> int:
    raw = os.environ.get(ENV_POOL_MAX_SIZE, "").strip()
    if not raw:
        return _DEFAULT_POOL_MAX_SIZE
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using default %d",
            ENV_POOL_MAX_SIZE, raw, _DEFAULT_POOL_MAX_SIZE,
        )
        return _DEFAULT_POOL_MAX_SIZE
    return max(_POOL_MIN_SIZE, value)


def get_pool() -> ConnectionPool:
    """Return the process-wide pool, creating it on first call.

    Raises ``RuntimeError`` when ``DATABASE_URL`` is unset — callers must
    check :func:`is_configured` first (the session-store facade does).
    """
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        url = database_url()
        if url is None:
            raise RuntimeError(f"{ENV_DATABASE_URL} is not set")
        from psycopg_pool import ConnectionPool

        max_size = _pool_max_size()
        pool: ConnectionPool = ConnectionPool(
            conninfo=url,
            min_size=_POOL_MIN_SIZE,
            max_size=max_size,
            timeout=_POOL_WAIT_TIMEOUT_S,
            max_idle=_POOL_MAX_IDLE_S,
            # Naive timestamps written by the app are UTC (``Session.created_at``
            # uses ``datetime.utcnow``); pin the session timezone so Postgres
            # never reinterprets them in a server-local zone.
            kwargs={"options": "-c timezone=UTC", "application_name": "openexecutive"},
            open=True,
        )
        _pool = pool
        logger.info("chat history: Postgres pool opened (max_size=%d)", max_size)
        return pool


def close_pool() -> None:
    """Close the pool (app shutdown / tests). Safe to call when never opened."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.close()
        logger.info("chat history: Postgres pool closed")


@contextmanager
def connection() -> Iterator[Connection[Any]]:
    """Check a connection out of the pool for one unit of work.

    The block runs in a transaction: it commits on normal exit and rolls
    back if the body raises, then the connection returns to the pool.
    """
    with get_pool().connection() as conn:
        yield conn
