# App-wide startup/shutdown hooks.
#
# Everything the app needs to have open before it serves a request, and closed
# before the process exits, is opened and closed here. Two things qualify: the
# Postgres connection pool and the shared HTTP client.
#
# Doing it here rather than lazily on first use means a bad DATABASE_URL fails
# the boot with a clear error, instead of passing a health check and then
# failing the first request that touches the database.

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.integrations.db import close_pool, open_pool
from src.integrations.http import close_client, open_client

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("App starting up...")
    await open_pool()
    await open_client()
    try:
        yield
    finally:
        # `finally`, so a crash while serving still drains the pool and closes
        # the sockets rather than leaving them for the OS to reap.
        await close_client()
        await close_pool()
        logger.info("App shut down")
