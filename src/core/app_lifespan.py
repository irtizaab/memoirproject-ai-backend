# App-wide startup/shutdown hooks (e.g. warming up connections, background tasks).

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("App starting up...")
    yield
    logger.info("App shutting down...")
