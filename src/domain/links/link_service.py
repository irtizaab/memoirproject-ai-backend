# Domain layer for shareable links.
#
# Its own feature folder rather than part of memoirs/ because the caller is a
# different person with different rights: a contributor who has no account and
# never will. The product constraint "contributors never create accounts" is
# what this file exists to serve, and keeping it separate makes it obvious that
# nothing here may assume a user_id.

import logging

from src.integrations.db import db

logger = logging.getLogger(__name__)


def resolve_link(token: str) -> dict | None:
    """Look up the memoir a share token points at, and count the open.

    Returns just enough for the invitation screen, or None if the token is
    unknown or revoked. The caller cannot tell those two apart, which is the
    point - a revoked link must look exactly like one that never existed.

    The token in the URL is the entire credential here. There is no account to
    check it against, which is why the tokens are 24 random bytes from
    gen_random_bytes: long enough that guessing one is not a strategy.

    ---------------------------------------------------------------------
    Why this is one statement
    ---------------------------------------------------------------------
    The UPDATE sits inside a CTE - a WITH clause, a named temporary result the
    rest of the query can select from. Postgres runs the UPDATE, and the SELECT
    below joins onto whatever it returned.

    Doing it this way means the counter increments and the memoir is read in a
    single trip, and the two can never disagree. Written as an UPDATE followed
    by a separate SELECT, a link revoked in between would be counted as opened
    and then reported as missing.

    `revoked_at IS NULL` inside the UPDATE is what makes revocation actually
    work: a killed token matches no row, so nothing is returned and the counter
    does not move.
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH opened AS (
                UPDATE memoir_link
                   SET open_count = open_count + 1
                 WHERE token = %(token)s
                   AND revoked_at IS NULL
             RETURNING memoir_id, scope::text AS scope
            )
            SELECT o.memoir_id,
                   o.scope,
                   m.subject_name,
                   m.born_year,
                   m.through_year,
                   m.subject_is_living,
                   COALESCE(p.display_name, '') AS invited_by
              FROM opened o
              JOIN memoir m
                ON m.id = o.memoir_id
              LEFT JOIN memoir_participant p
                     ON p.memoir_id = o.memoir_id
                    AND p.role = 'owner'
            """,
            {"token": token},
        )
        row = cur.fetchone()

    if row is None:
        # Deliberately info, not warning. A stale link being opened is an
        # ordinary event - someone found an old WhatsApp message - not a sign
        # anything is wrong with the system.
        logger.info("Link token did not resolve")

    return row
