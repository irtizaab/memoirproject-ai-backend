# Integrations = thin wrappers around external services. Postgres is an external
# service, so this is where "how do I talk to the database" lives — and the ONLY
# place it lives.
#
# Nothing in here knows what a draft is. If you ever find yourself typing the
# word "memoir" in this file, the code you're writing belongs in domain/ instead.

import logging

import psycopg
from psycopg.rows import dict_row

from src.core.config import settings

logger = logging.getLogger(__name__)


def db():
    """Open a new connection to Postgres.

    Moved verbatim from the old main.py. Used as a context manager, which is
    what makes the transaction work:

        with db() as conn, conn.cursor() as cur:
            cur.execute(...)

    Leaving that `with` block commits the transaction (or rolls it back if an
    exception escaped) and closes the connection.

    `row_factory=dict_row` is why cur.fetchone() gives you {"id": ...} instead
    of a bare tuple — that's what lets the route handlers return the row
    straight to FastAPI as JSON.

    Note this opens a *fresh* connection every call, with no pooling. Fine at
    current traffic; when it stops being fine, a connection pool goes here and
    every caller keeps working unchanged. That is the payoff for putting this
    in one file.
    """
    return psycopg.connect(settings.database_url, row_factory=dict_row)


def ping() -> int:
    """Cheapest possible round-trip to prove the database is reachable.

    Returns 1. The value is meaningless — the point is that getting it back at
    all means the connection string works, the network is up, and Postgres is
    answering. If the database is down this raises, and /health returns a 500,
    which is exactly what a health check is supposed to do.
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        row = cur.fetchone()
    return row["ok"]
