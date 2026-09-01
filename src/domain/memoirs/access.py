# Who is allowed to touch which memoir.
#
# Two callers can legitimately write to a memoir, and they prove it in
# completely different ways:
#
#   the owner        a Supabase JWT, checked against memoir.created_by_user_id
#   a contributor    a share token, checked against memoir_link
#
# Both answers are needed by memories and by media, so they live here rather
# than being written twice and drifting apart. Every function takes an open
# cursor so it composes inside a caller's transaction — an authorization check
# that commits separately from the write it authorizes is a race waiting to
# happen.
#
# All of them return None rather than raising on "not allowed". The API layer
# turns that into 404 — never 403 — so a stranger poking at memoir ids cannot
# learn which ones exist.

import logging

logger = logging.getLogger(__name__)


def owned_memoir(cur, memoir_id: str, user_id: str) -> dict | None:
    """The memoir, if this user owns it. None otherwise.

    Returns `status` too, because almost every caller needs to know whether the
    memoir is published before it writes.
    """
    cur.execute(
        """
        SELECT id, subject_name, status::text AS status
          FROM memoir
         WHERE id = %(memoir_id)s
           AND created_by_user_id = %(user_id)s
        """,
        {"memoir_id": memoir_id, "user_id": user_id},
    )
    return cur.fetchone()


def owned_memoir_of_memory(cur, memory_id: str, user_id: str) -> dict | None:
    """The memoir a memory belongs to, if this user owns it.

    The edit and delete routes are addressed by memory id alone — `/memories/
    {id}` — so ownership has to be reached by joining back up to the memoir.
    """
    cur.execute(
        """
        SELECT m.id, m.status::text AS status
          FROM memory mem
          JOIN memoir m ON m.id = mem.memoir_id
         WHERE mem.id = %(memory_id)s
           AND m.created_by_user_id = %(user_id)s
        """,
        {"memory_id": memory_id, "user_id": user_id},
    )
    return cur.fetchone()


def contributable_memoir(cur, link_token: str) -> dict | None:
    """The memoir a live contribute link points at. None if it cannot be used.

    Four things have to hold, and failing any of them looks identical from
    outside:

      - the token exists
      - the link has not been revoked
      - its scope is `contribute`, not `view`
      - the memoir is still a draft

    The last is the immutability rule: once a memoir is published its contents
    can never change, and a link that was live before publication must stop
    accepting material the moment it happens.

    Note this does **not** bump `open_count`. That belongs to resolving the
    link for display, in `domain/links/`; counting a submission as an open
    would inflate the number the owner reads as "how many people looked".
    """
    cur.execute(
        """
        SELECT m.id, m.status::text AS status
          FROM memoir_link l
          JOIN memoir m ON m.id = l.memoir_id
         WHERE l.token = %(token)s
           AND l.revoked_at IS NULL
           AND l.scope = 'contribute'
           AND m.status = 'draft'
        """,
        {"token": link_token},
    )
    return cur.fetchone()
