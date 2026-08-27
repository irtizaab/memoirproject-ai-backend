# App-wide error handling. Registered once from main.py on startup.
#
# Why this exists: Postgres enforces real product rules through CHECK
# constraints, enums and unique indexes (see migrations/0001_slice1.sql). When
# a request violates one, psycopg raises. Without a handler, FastAPI turns that
# into a bare 500 with a stack trace — which reads as "the server is broken"
# when the truth is "you sent something the rules don't allow".
#
# This translates the database's own error codes into honest status codes.

import logging

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def handle_db_error(request: Request, exc: psycopg.Error):
    """Map a Postgres error code (SQLSTATE) to a client-facing response.

    SQLSTATE is a five-character code every Postgres error carries. It is
    stable across versions and far more reliable to branch on than the human
    readable message text, which changes.
    """
    code = exc.sqlstate

    # 23514 — CHECK constraint violation. A product rule written into the
    # schema was broken, e.g. draft_living_has_no_end_year, or a born_year
    # later than a through_year. The constraint name is returned so the
    # frontend can point at the field that caused it.
    if code == "23514":
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_data",
                "constraint": exc.diag.constraint_name,
            },
        )

    # 22P02 — invalid text representation. Most often a value that isn't a
    # member of an enum, e.g. relationship="cousin" when relationship_group
    # only allows child/grandchild/spouse_partner/friend/self/other.
    if code == "22P02":
        return JSONResponse(status_code=400, content={"error": "invalid_value"})

    # 23505 — unique violation. Something that must be one-of-a-kind already
    # exists, e.g. a second owner on the same memoir.
    if code == "23505":
        return JSONResponse(status_code=409, content={"error": "already_exists"})

    # 23503 — foreign key violation. A row pointed at a parent that isn't there.
    if code == "23503":
        return JSONResponse(status_code=400, content={"error": "invalid_reference"})

    # Anything else is not a rule the client broke — it is a bug, a permissions
    # problem, or the database being down. Re-raising lets it become a real 500
    # with a full traceback. Swallowing it here would hide exactly the failures
    # worth knowing about.
    raise exc


def register_error_handlers(app: FastAPI) -> None:
    """Attach the handlers above to the app. Called once from main.py."""
    app.add_exception_handler(psycopg.Error, handle_db_error)
