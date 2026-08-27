# Domain layer = the business logic for a feature, plus the SQL that backs it.
#
# This file does not import fastapi. It returns None or raises its own
# exceptions; turning those into 404s and 400s is the API layer's job.

import logging

from src.integrations.db import db

logger = logging.getLogger(__name__)


class DraftIncomplete(Exception):
    """The draft cannot become a memoir because a required answer is missing.

    Distinct from "draft not found" because it means something different to
    the user: not "this link is wrong" but "go back and answer the name
    question". The API layer maps this to 400 and the other to 404.
    """


def claim_draft(
    draft_id: str,
    draft_token: str,
    user_id: str,
    email: str,
    full_name: str,
) -> dict | None:
    """Turn an anonymous draft into a real memoir owned by a real account.

    This is the hinge of the whole onboarding flow. Everything before it is
    anonymous and disposable; everything after belongs to somebody.

    Four rows are written and they only make sense together:

      1. user_account       - the owner's account row
      2. memoir             - the memoir itself
      3. memoir_participant - the owner's presence in that memoir
      4. memoir_link        - the shareable contribute link

    Returns the new memoir plus its link token, or None if the draft could not
    be claimed. Raises DraftIncomplete if the draft has no subject name.

    ---------------------------------------------------------------------
    Why this is one transaction
    ---------------------------------------------------------------------
    A transaction is a group of statements the database treats as a single
    all-or-nothing unit. Either every row lands, or none of them do.

    Here that is not tidiness, it is correctness. Suppose the memoir INSERT
    succeeds and the participant INSERT then fails. You are left with a memoir
    that has no owner row - nobody can open it, nobody can be shown as its
    owner, and no code path will ever clean it up, because every query that
    would find it starts from a participant. An orphan like that is invisible
    and permanent.

    The `with db() as conn:` block is what makes it atomic. psycopg commits
    when the block exits normally and rolls back if any exception escapes it.
    Note the shape: ONE connection block wrapping every statement. Four
    separate `with db()` blocks would be four separate transactions and would
    give exactly the broken behaviour described above.

    ---------------------------------------------------------------------
    Why the UPDATE comes before the INSERTs
    ---------------------------------------------------------------------
    Step 2 both reads the draft's answers and marks it claimed, in a single
    statement, filtered on `claimed_at IS NULL`.

    That ordering is the guard against a double-submit - an impatient user
    double-clicking "Save the memoir", or the browser retrying. Two concurrent
    requests both run that UPDATE; Postgres serialises them on the row, and
    the second one matches nothing because the first already set claimed_at.
    It gets None and creates no memoir. Reading first and updating later would
    leave a window where both requests see an unclaimed draft and both build a
    memoir, and the family ends up with two.
    """
    params = {
        "draft_id": draft_id,
        "token": draft_token,
        "user_id": user_id,
        "email": email or None,
        "full_name": full_name or "",
    }

    with db() as conn:
        with conn.cursor() as cur:
            # 1. Make sure the account row exists.
            #
            # memoir.created_by_user_id has a foreign key to user_account(id),
            # so the account must exist before the memoir can. The user signed
            # up with Supabase, which created their auth.users row; this is the
            # first time our own tables hear about them.
            #
            # ON CONFLICT DO NOTHING makes this safe to run on every claim: the
            # second memoir a person creates finds their account already there
            # and moves on, instead of failing on the primary key.
            cur.execute(
                """
                INSERT INTO user_account (id, email, full_name)
                VALUES (%(user_id)s, %(email)s, %(full_name)s)
                ON CONFLICT (id) DO NOTHING
                """,
                params,
            )

            # 2. Claim the draft and read its answers in one atomic step.
            cur.execute(
                """
                UPDATE memoir_draft
                   SET claimed_at = now()
                 WHERE id = %(draft_id)s
                   AND token = %(token)s
                   AND claimed_at IS NULL
             RETURNING subject_name,
                       relationship::text AS relationship,
                       relationship_label,
                       born_year, through_year, subject_is_living, never_forget
                """,
                params,
            )
            draft = cur.fetchone()

            # None covers "no such draft", "wrong token" and "already claimed"
            # alike, and the caller is told the same thing for all three.
            if draft is None:
                return None

            # 3. The one answer that is not optional.
            #
            # memoir.subject_name is NOT NULL with a CHECK that it isn't blank,
            # while memoir_draft.subject_name is nullable - someone can close
            # the tab on question one. So a draft can legitimately exist that
            # cannot become a memoir, and this is where that is caught.
            #
            # Raising here rolls the whole transaction back, including the
            # claimed_at we just set. That matters: the draft stays claimable,
            # so the user can go back, type the name, and try again.
            subject_name = (draft["subject_name"] or "").strip()
            if not subject_name:
                raise DraftIncomplete("draft has no subject name")

            # 4. The memoir itself.
            cur.execute(
                """
                INSERT INTO memoir (subject_name, born_year, through_year,
                                    subject_is_living, never_forget,
                                    created_by_user_id)
                VALUES (%(subject_name)s, %(born_year)s, %(through_year)s,
                        %(subject_is_living)s, %(never_forget)s, %(user_id)s)
             RETURNING id, subject_name, born_year, through_year,
                       subject_is_living, never_forget,
                       status::text AS status, created_at
                """,
                {
                    "subject_name": subject_name,
                    "born_year": draft["born_year"],
                    "through_year": draft["through_year"],
                    "subject_is_living": draft["subject_is_living"],
                    "never_forget": draft["never_forget"],
                    "user_id": user_id,
                },
            )
            memoir = cur.fetchone()

            # 5. The owner's participant row.
            #
            # Everyone in a memoir has one of these, owner and contributor
            # alike, so that later tables (memories, media) can point at a
            # single "who added this" table regardless of whether that person
            # has an account.
            #
            # display_name has a NOT NULL CHECK that it isn't blank, so fall
            # back through the name from the token, then the local part of the
            # email, then a constant - never an empty string.
            #
            # `relationship` is the OWNER's relationship to the subject, which
            # is exactly what the onboarding asked for on the second screen.
            # The column is NOT NULL DEFAULT 'other', but a DEFAULT only
            # applies when the column is left out entirely - passing NULL
            # explicitly would violate the constraint, hence the `or "other"`.
            display_name = (
                full_name.strip()
                or (email.split("@")[0] if email else "")
                or "Owner"
            )
            cur.execute(
                """
                INSERT INTO memoir_participant (memoir_id, role, user_id,
                                                display_name, relationship,
                                                relationship_label, email)
                VALUES (%(memoir_id)s, 'owner', %(user_id)s, %(display_name)s,
                        %(relationship)s::relationship_group,
                        %(relationship_label)s, %(email)s)
                """,
                {
                    "memoir_id": memoir["id"],
                    "user_id": user_id,
                    "display_name": display_name,
                    "relationship": draft["relationship"] or "other",
                    "relationship_label": draft["relationship_label"],
                    "email": email or None,
                },
            )

            # 6. The link the whole product is built around.
            #
            # scope 'contribute' is the one the dashboard shows. A partial
            # unique index allows only one live link per scope per memoir,
            # which is what keeps the promise of "one link".
            cur.execute(
                """
                INSERT INTO memoir_link (memoir_id, scope)
                VALUES (%(memoir_id)s, 'contribute')
             RETURNING token
                """,
                {"memoir_id": memoir["id"]},
            )
            link = cur.fetchone()

    # Reached only if the block above committed.
    logger.info("Claimed draft %s into memoir %s", draft_id, memoir["id"])
    return {**memoir, "link_token": link["token"]}


def list_memoirs_for_owner(user_id: str) -> list[dict]:
    """Every memoir this account created, newest first.

    LEFT JOIN, not JOIN: a memoir whose link has been revoked still belongs on
    the owner's dashboard. An inner join would make it vanish from their own
    account, which is worse than showing it with no link to copy.

    The join is filtered to live contribute links only - `revoked_at IS NULL` -
    so a dead token is never handed back to be shared.
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.subject_name, m.born_year, m.through_year,
                   m.subject_is_living, m.never_forget,
                   m.status::text AS status, m.created_at,
                   l.token AS link_token
              FROM memoir m
              LEFT JOIN memoir_link l
                     ON l.memoir_id = m.id
                    AND l.scope = 'contribute'
                    AND l.revoked_at IS NULL
             WHERE m.created_by_user_id = %(user_id)s
             ORDER BY m.created_at DESC
            """,
            {"user_id": user_id},
        )
        return cur.fetchall()
