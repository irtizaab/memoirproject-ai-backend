# Domain layer for the people in a memoir, and the link that let them in.
#
# Read-only apart from `reissue_link` and `merge_participants`. The owner does
# not create contributors — they arrive by opening a link and leaving something
# — so there is no "add contributor" here, and there should not be. The two
# things an owner can do to this list are stop it growing, by killing the link,
# and say that two entries are one person.

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
               -- A merged row is not a person, it is a spare key. It keeps a
               -- second device's token working and is reached only by
               -- following that token; it must never be a line on this list,
               -- or merging two duplicates would leave three.
               AND p.merged_into IS NULL
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


class CannotMerge(Exception):
    """The two participants named cannot be merged into one.

    Covers the cases that are wrong rather than merely not found: merging
    somebody into themselves, and merging the owner into a contributor or the
    reverse. Both are the caller asking for something incoherent, so the route
    answers 400 rather than 404 — unlike a participant that is simply not in
    this memoir, which stays indistinguishable from one that does not exist.
    """


def merge_participants(
    memoir_id: str, user_id: str, loser_id: str, winner_id: str
) -> dict | None:
    """Record that two contributor entries are one person. None if not yours.

    The duplicate this fixes is the same human on a second device: identity is
    a token in a browser, so a phone and a laptop are two participants with two
    tokens and no way for the API to know they are one aunt.

    **Nothing here guesses.** The owner names both rows; matching on
    `display_name` would merge two cousins who are both called Ali, and there
    would be no way to notice it had happened. The API only records a decision
    somebody with actual knowledge already made.

    ---------------------------------------------------------------------
    Why the loser is kept
    ---------------------------------------------------------------------
    Because their device still holds their token. Deleting the row would leave
    that token resolving to nothing, and the next memory from that phone would
    create a *third* participant — the original bug, back again, silently.

    So the row stays, pointing at the winner, and `resolve_participant`
    follows the pointer. Both devices keep working and both lead to one person.

    ---------------------------------------------------------------------
    Why chains cannot form
    ---------------------------------------------------------------------
    One hop is only enough if nothing ever points at something already merged.
    Two guards keep it that way: merging *into* a merged row is refused, and
    anything already pointing at the loser is re-pointed at the winner in the
    same statement. Both are cheap; an unbounded loop over a column that could
    become circular would be a hang in the one endpoint contributors reach.

    One transaction throughout. A merge that moved the memories and then failed
    to mark the row would show one person's memories under a name that is still
    listed twice.
    """
    if loser_id == winner_id:
        raise CannotMerge("a participant cannot be merged into itself")

    with db() as conn, conn.cursor() as cur:
        if owned_memoir(cur, memoir_id, user_id) is None:
            return None

        # Both rows, in one read, scoped to this memoir. `FOR UPDATE` because
        # two merges racing on the same pair could otherwise each see the other
        # as unmerged and produce the chain the docstring rules out.
        cur.execute(
            """
            SELECT id, role::text AS role, display_name,
                   merged_into, first_opened_at
              FROM memoir_participant
             WHERE memoir_id = %(memoir_id)s
               AND id IN (%(loser_id)s, %(winner_id)s)
               FOR UPDATE
            """,
            {
                "memoir_id": memoir_id,
                "loser_id": loser_id,
                "winner_id": winner_id,
            },
        )
        rows = {str(row["id"]): row for row in cur.fetchall()}

        loser = rows.get(loser_id)
        winner = rows.get(winner_id)
        if loser is None or winner is None:
            # One of them is not in this memoir, or does not exist. The same
            # 404 either way, as everywhere else.
            return None

        if loser["role"] == "owner" or winner["role"] == "owner":
            raise CannotMerge("the owner is not a duplicate of anyone")

        if winner["merged_into"] is not None:
            raise CannotMerge(
                "that entry has already been merged into another one"
            )

        # Move the memories. Filtered on `memoir_id` as well as the participant
        # so a stray id cannot reach across memoirs, the same as everywhere.
        cur.execute(
            """
            UPDATE memory
               SET participant_id = %(winner_id)s
             WHERE memoir_id = %(memoir_id)s
               AND participant_id = %(loser_id)s
            """,
            {
                "memoir_id": memoir_id,
                "winner_id": winner_id,
                "loser_id": loser_id,
            },
        )
        moved = cur.rowcount

        # Anything that already pointed at the loser now points at the winner,
        # so no row is ever two hops from the person it belongs to.
        cur.execute(
            """
            UPDATE memoir_participant
               SET merged_into = %(winner_id)s
             WHERE memoir_id = %(memoir_id)s
               AND merged_into = %(loser_id)s
            """,
            {
                "memoir_id": memoir_id,
                "winner_id": winner_id,
                "loser_id": loser_id,
            },
        )

        cur.execute(
            """
            UPDATE memoir_participant
               SET merged_into = %(winner_id)s
             WHERE memoir_id = %(memoir_id)s
               AND id = %(loser_id)s
            """,
            {
                "memoir_id": memoir_id,
                "winner_id": winner_id,
                "loser_id": loser_id,
            },
        )

        # The earlier of the two first opens. They are one person, and the
        # honest answer to "when did they first open the link" is whichever
        # device did it first.
        cur.execute(
            """
            UPDATE memoir_participant
               SET first_opened_at = LEAST(
                       COALESCE(first_opened_at, %(loser_opened)s),
                       COALESCE(%(loser_opened)s, first_opened_at)
                   )
             WHERE memoir_id = %(memoir_id)s
               AND id = %(winner_id)s
            """,
            {
                "memoir_id": memoir_id,
                "winner_id": winner_id,
                "loser_opened": loser["first_opened_at"],
            },
        )

    logger.info(
        "Merged participant %s into %s (%s memories moved)",
        loser_id,
        winner_id,
        moved,
    )
    return {"participant_id": winner_id, "memories_moved": moved}


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
