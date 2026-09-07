# Integrations = thin wrappers around external services. Postgres is an external
# service, so this is where "how do I talk to the database" lives — and the ONLY
# place it lives.
#
# Nothing in here knows what a draft is. If you ever find yourself typing the
# word "memoir" in this file, the code you're writing belongs in domain/ instead.
#
# ---------------------------------------------------------------------------
# Why this is async, and why it is pooled
#
# The app is async end to end. A synchronous driver call inside an `async def`
# handler blocks the event loop — not just that request, but every other
# request the process is serving — so `psycopg.AsyncConnection` is not a style
# preference here, it is the only correct choice given the handlers.
#
# The pool is the other half. Opening a fresh connection per request means a
# TCP handshake, a TLS handshake and an auth round-trip on every single call,
# and as many sockets against Postgres as there are requests in flight. The
# pool pays that cost once at startup and hands out ready connections; when it
# is empty, callers queue instead of opening socket number 200. That queue is
# the backpressure that keeps the database up under a traffic spike.
# ---------------------------------------------------------------------------

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from src.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Windows needs the selector loop, and this is the module that needs it
#
# Python on Windows defaults to ProactorEventLoop, and psycopg's async driver
# refuses to run on it. Production is Linux and never sees this; developers on
# Windows see it constantly, and the symptom is bad — the pool retries quietly
# and then times out after ten seconds, which reads as "the database is down".
#
# Setting the policy covers everything that starts its own loop: pytest, a
# script, `asyncio.run`. It does NOT cover uvicorn, which since 0.50 picks a
# loop *factory* directly and ignores the policy. `check_event_loop()` below is
# what catches that case, because a wrong loop has to be reported, not fixed:
# by the time the pool opens, the loop is already running.
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def check_event_loop() -> None:
    """Fail loudly on a loop psycopg cannot use, instead of timing out on it.

    uvicorn on Windows gives a single process a ProactorEventLoop, and a pool
    on one never connects. It hands `--reload` and `--workers` runs a
    SelectorEventLoop instead, which works — so the fix is a flag, and this
    says which one rather than leaving a ten-second silence to interpret.
    """
    if sys.platform != "win32":
        return
    loop = asyncio.get_running_loop()
    if isinstance(loop, asyncio.ProactorEventLoop):
        raise RuntimeError(
            "psycopg's async driver cannot run on Windows' ProactorEventLoop, "
            "which is what uvicorn gives a single process. Run it with "
            "`--reload` (development) or `--workers 2` (production-like) and "
            "uvicorn uses the selector loop instead. Linux is unaffected."
        )

# One pool per process. `main.py` runs uvicorn with several worker processes,
# so the real ceiling on connections is workers x db_pool_max_size — size the
# two together against whatever Postgres allows.
_pool: AsyncConnectionPool | None = None

# Guards the lazy open below. Without it, two coroutines arriving at once on a
# cold pool would each build one and the second would silently orphan the first.
_pool_lock = asyncio.Lock()


async def open_pool() -> AsyncConnectionPool:
    """Build the pool and prove it can reach Postgres. Idempotent.

    Called by the app lifespan at startup so that a bad DATABASE_URL fails the
    boot rather than the first request. `wait=True` is what makes that true: it
    blocks until `min_size` connections are actually established, so a URL that
    points nowhere raises here instead of looking healthy until traffic lands.
    """
    global _pool
    async with _pool_lock:
        if _pool is not None:
            return _pool
        check_event_loop()
        pool = AsyncConnectionPool(
            settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            # Applied to every connection the pool hands out. This is why
            # cur.fetchone() gives you {"id": ...} rather than a bare tuple,
            # which is what lets a row be returned straight to FastAPI as JSON.
            kwargs={"row_factory": dict_row},
            # How long a caller waits for a free connection before giving up.
            # Bounded on purpose: a request that cannot get a connection should
            # fail fast as a 503 (see core/error_handlers.py), not hang until
            # the client times out and retries into the same jam.
            timeout=settings.db_pool_timeout,
            open=False,
        )
        await pool.open(wait=True, timeout=10)
        _pool = pool
        logger.info(
            "Postgres pool open (min=%s max=%s)",
            settings.db_pool_min_size,
            settings.db_pool_max_size,
        )
        return _pool


async def close_pool() -> None:
    """Drain and close the pool. Called by the lifespan on shutdown."""
    global _pool
    async with _pool_lock:
        if _pool is None:
            return
        await _pool.close()
        _pool = None
        logger.info("Postgres pool closed")


@asynccontextmanager
async def db():
    """Borrow a connection from the pool.

    Used as an async context manager, which is what makes the transaction work:

        async with db() as conn, conn.cursor() as cur:
            await cur.execute(...)

    Leaving that block commits the transaction (or rolls it back if an
    exception escaped) and returns the connection to the pool rather than
    closing it.

    Opens the pool on first use if the lifespan has not run. That is for tests
    and one-off scripts that call a domain function directly; in the server the
    lifespan has always got here first and this is a null check.
    """
    pool = _pool or await open_pool()
    async with pool.connection() as conn:
        yield conn


async def ping() -> int:
    """Cheapest possible round-trip to prove the database is reachable.

    Returns 1. The value is meaningless — the point is that getting it back at
    all means the pool has a live connection, the network is up, and Postgres
    is answering. If the database is down this raises, and /health returns a
    500, which is exactly what a health check is supposed to do.
    """
    async with db() as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1 AS ok")
        row = await cur.fetchone()
    return row["ok"]
