# Domain layer for the people in a memoir, and the link that let them in.
#
# Read-only apart from `reissue_link`. The owner does not create contributors —
# they arrive by opening a link and leaving something — so there is no "add
# contributor" here, and there should not be. The one thing an owner can do to
# this list is stop it growing, by killing the link.

import logging

from src.domain.memoirs.access import owned_memoir
from src.integrations.db import db

logger = logging.getLogger(__name__)


def list_contributors(memoir_id: str, user_id: str) -> dict | None:
    """Everyone in the memoir and the live link, for the contributors screen.

    One call rather than two, because the page shows them together and a
    second round trip would only make it flicker.

    Returns None if the memoir is not this user's.
    """
    with db() as conn, conn.cursor() as cur:
        if owned_memoir(cur, memoir_id, user_id) is None:
            return None

        # LEFT JOIN, not INNER: somebody who opened the link and has not
        # written anything yet is exactly who the owner most wants to see, and
        # an inner join would hide them.
        cur.execute(
            """
            SELECT p.id,
                   p.display_name,
                   p.role::text AS role,
                   p.relationship::text AS relationship,
                   p.relationship_label,
                   p.first_opened_at,
                   p.created_at,
                   COUNT(mem.id) AS memory_count,
                   MAX(mem.created_at) AS last_contribution_at
              FROM memoir_participant p
              LEFT JOIN memory mem
                     ON mem.memoir_id = p.memoir_id
                    AND mem.participant_id = p.id
             WHERE p.memoir_id = %(memoir_id)s
          GROUP BY p.id, p.display_name, p.role, p.relationship,
                   p.relationship_label, p.first_opened_at, p.created_at
             -- Owner first, then whoever contributed most recently. Someone
             -- who has added nothing sorts last, which is where the eye should
             -- land when deciding who to nudge.
          ORDER BY (p.role = 'owner') DESC,
                   MAX(mem.created_at) DESC NULLS LAST,
                   p.created_at
            """,
            {"memoir_id": memoir_id},
        )
        participants = cur.fetchall()

        cur.execute(
            """
            SELECT token, open_count, created_at
              FROM memoir_link
             WHERE memoir_id = %(memoir_id)s
               AND scope = 'contribute'
               AND revoked_at IS NULL
            """,
            {"memoir_id": memoir_id},
        )
        link = cur.fetchone()

    # `contributor_token` is deliberately not selected above. It is a
    # credential belonging to the contributor, and the owner has no use for it
    # that is not impersonation.
    return {"link": link, "participants": participants}


def reissue_link(memoir_id: str, user_id: str) -> dict | None:
    """Kill the current share link and issue a replacement.

    The remedy for a link that has been forwarded further than intended.
    Everyone holding the old URL loses access immediately; everyone who should
    still have it gets the new one from the owner.

    Both halves happen in one transaction, and in this order, because the
    partial unique index `memoir_link_one_live_per_scope` permits exactly one
    live contribute link per memoir. Inserting before revoking would violate
    it; doing them in separate transactions would leave a window with no live
    link at all.

    The old row is kept rather than deleted, so `open_count` on a link that had
    to be killed remains readable — which is the only evidence of how widely it
    travelled.
    """
    with db() as conn, conn.cursor() as cur:
        if owned_memoir(cur, memoir_id, user_id) is None:
            return None

        cur.execute(
            """
            UPDATE memoir_link
               SET revoked_at = now()
             WHERE memoir_id = %(memoir_id)s
               AND scope = 'contribute'
               AND revoked_at IS NULL
            """,
            {"memoir_id": memoir_id},
        )

        cur.execute(
            """
            INSERT INTO memoir_link (memoir_id, scope)
            VALUES (%(memoir_id)s, 'contribute')
            RETURNING token, open_count, created_at
            """,
            {"memoir_id": memoir_id},
        )
        return cur.fetchone()
