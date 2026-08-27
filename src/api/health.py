# API layer = FastAPI routes. This one confirms the service is alive and can
# still reach Postgres.

import logging

from fastapi import APIRouter

from src.integrations.db import ping

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    """Report whether the API can reach the database.

    Note this route calls integrations/ directly, skipping domain/ — the one
    deliberate exception to the layering rule. There is no business decision
    involved in a one-row liveness query; adding a domain module just to
    forward a single call would be ceremony with no payoff.

    If the database is unreachable, ping() raises and this returns a 500,
    which is what a health check is supposed to do. Do not wrap it in a
    try/except that reports "ok" anyway.
    """
    return {"status": "ok", "database": ping()}
