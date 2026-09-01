# Domain layer for memories — the things people were invited to leave behind.
#
# Nothing here imports fastapi. These functions take plain values, return plain
# dicts, and signal failure by returning None or raising a domain exception.
# Deciding that None means 404 and MemoirPublished means 409 is the API layer's
# job, in src/api/memories.py.

import logging

from src.domain.memoirs.access import (
    contributable_memoir,
    owned_memoir,
    owned_memoir_of_memory,
)
from src.domain.transcripts.transcript_service import (
    to_payload,
    transcripts_for_assets,
)
from src.integrations.db import db
from src.integrations.supabase_storage import (
    create_signed_download_urls,
    delete_object,
)

logger = logging.getLogger(__name__)


class EmptyMemory(Exception):
    """A memory that holds nothing at all.

    No writing, no photograph, no recording. Rejected here rather than left to
    the database, where it would surface as a `memory_text_has_body` violation
    — a constraint whose name explains nothing to the person who just pressed
    Save on a blank form. The route maps this to 400: malformed input, not a
    conflict with anything.
    """


class MemoirPublished(Exception):
    """The memoir is published, and published memoirs never change.

    Not a validation error and not a permissions error — the caller is the
    rightful owner and the request is well-formed. It is a conflict with the
    current state of the resource, which is what 409 means.

    This is the product's hardest rule: "Once a memoir's status flips to
    published, its content can never be edited — not by the owner, not by an
    admin." Everything that writes checks it.
    """


# The columns every memory query returns, so the shape reaching the API is the
# same whichever route asked. Written once because three queries need it and
# three copies would drift.
_MEMORY_COLUMNS = """
    mem.id,
    mem.memoir_id,
    mem.kind::text AS kind,
    mem.title,
    mem.body_text,
    mem.happened_on,
    mem.created_at,
    p.display_name AS contributor_name,
    p.relationship::text AS contributor_relationship
"""


def _attach_assets(cur, memories: list[dict]) -> list[dict]:
    """Hang each memory's media on it, with freshly signed URLs and transcripts.

    Three round trips regardless of how many memories there are: one query for
    every asset belonging to the set, one batch call to storage to sign them,
    and one query for every transcript. Doing any of it per memory would turn
    an archive of twelve into twenty-five sequential requests.
    """
    if not memories:
        return memories

    ids = [m["id"] for m in memories]
    cur.execute(
        """
        SELECT id, memory_id, kind::text AS kind, mime_type,
               byte_size, duration_ms, storage_path
          FROM media_asset
         WHERE memory_id = ANY(%(ids)s)
           AND uploaded_at IS NOT NULL
         ORDER BY created_at
        """,
        {"ids": ids},
    )
    assets = cur.fetchall()

    # Sign every path in one call, then hand each asset its own URL. An asset
    # storage could not sign gets url=None and renders as a gap rather than
    # taking the page down.
    signed = create_signed_download_urls([a["storage_path"] for a in assets])

    # One more query, not one per asset. Only audio can have a transcript, so
    # a memoir of photographs asks for nothing.
    audio_ids = [a["id"] for a in assets if a["kind"] == "audio"]
    transcripts = transcripts_for_assets(cur, audio_ids)

    by_memory: dict = {}
    for asset in assets:
        asset["url"] = signed.get(asset.pop("storage_path"))
        asset["transcript"] = to_payload(transcripts.get(str(asset["id"])))
        by_memory.setdefault(asset["memory_id"], []).append(asset)

    for memory in memories:
        memory["assets"] = by_memory.get(memory["id"], [])

    return memories


def _adopt_assets(cur, memoir_id: str, memory_id: str, asset_ids: list[str]) -> None:
    """Point already-uploaded assets at the memory that now owns them.

    The `memoir_id` in the WHERE clause is the part that matters. Without it, a
    client could pass an asset id belonging to a different memoir and graft
    someone else's photograph onto its own memory. With it, the update simply
    matches nothing.

    `memory_id IS NULL` stops an asset being moved from one memory to another,
    which is not a thing any flow needs to do.
    """
    if not asset_ids:
        return

    cur.execute(
        """
        UPDATE media_asset
           SET memory_id = %(memory_id)s
         WHERE id = ANY(%(asset_ids)s)
           AND memoir_id = %(memoir_id)s
           AND memory_id IS NULL
        """,
        {
            "memory_id": memory_id,
            "asset_ids": asset_ids,
            "memoir_id": memoir_id,
        },
    )


def _derive_kind(cur, memoir_id: str, asset_ids: list[str], body_text: str | None) -> str:
    """What kind of memory this is, worked out from what it actually holds.

    A memory is no longer *one* thing. Someone can attach three photographs, two
    recordings and a paragraph to a single afternoon, which is how people
    actually remember. So `kind` stopped being a choice the client makes and
    became a description of the contents:

        any audio            -> 'voice'
        no audio, any image  -> 'photo'
        neither              -> 'text'

    It is the **primary medium**, and its only job is the eyebrow on an archive
    card. That is also why there is no 'mixed' value: it would need a migration
    for a word nobody should have to read above their grandmother's memory.

    Derived here rather than trusted from the request, so the answer cannot
    disagree with the row. The asset query is filtered on `memoir_id` for the
    same reason `_adopt_assets` is — an id belonging to somebody else's memoir
    matches nothing and therefore cannot influence the result.

    Raises EmptyMemory when there is nothing at all. Note how that guard and
    the database's `memory_text_has_body` agree by construction: 'text' is
    derived only when no asset survived the filter, so a 'text' row must carry
    words or it would have been rejected a line earlier.
    """
    kinds: set[str] = set()

    if asset_ids:
        cur.execute(
            """
            SELECT DISTINCT kind::text AS kind
              FROM media_asset
             WHERE id = ANY(%(asset_ids)s)
               AND memoir_id = %(memoir_id)s
               AND memory_id IS NULL
            """,
            {"asset_ids": asset_ids, "memoir_id": memoir_id},
        )
        kinds = {row["kind"] for row in cur.fetchall()}

    if not kinds and not (body_text or "").strip():
        raise EmptyMemory

    if "audio" in kinds:
        return "voice"
    if "image" in kinds:
        return "photo"
    return "text"


def _insert_memory(cur, memoir_id: str, participant_id: str, payload: dict) -> dict:
    """Write the row and read it back with its contributor's name."""
    asset_ids = payload.get("asset_ids") or []
    kind = _derive_kind(cur, memoir_id, asset_ids, payload.get("body_text"))

    cur.execute(
        """
        INSERT INTO memory (memoir_id, participant_id, kind, title,
                            body_text, happened_on)
        VALUES (%(memoir_id)s, %(participant_id)s, %(kind)s::memory_kind,
                %(title)s, %(body_text)s, %(happened_on)s)
        RETURNING id
        """,
        {
            "memoir_id": memoir_id,
            "participant_id": participant_id,
            "kind": kind,
            "title": payload.get("title"),
            "body_text": payload.get("body_text"),
            "happened_on": payload.get("happened_on"),
        },
    )
    memory_id = cur.fetchone()["id"]

    _adopt_assets(cur, memoir_id, str(memory_id), asset_ids)

    cur.execute(
        f"""
        SELECT {_MEMORY_COLUMNS}
          FROM memory mem
          JOIN memoir_participant p
            ON p.memoir_id = mem.memoir_id AND p.id = mem.participant_id
         WHERE mem.id = %(memory_id)s
        """,
        {"memory_id": memory_id},
    )
    return cur.fetchone()


# ---------------------------------------------------------------------------
# The owner's side
# ---------------------------------------------------------------------------


def list_memories(memoir_id: str, user_id: str) -> list[dict] | None:
    """Every memory in a memoir, newest first. None if it is not yours.

    Newest first because "Recent memories" on the archive means recently
    *added*, not most recently lived through. A memory of 1988 contributed this
    morning is news to the owner; one of 2002 added last year is not.
    """
    with db() as conn, conn.cursor() as cur:
        if owned_memoir(cur, memoir_id, user_id) is None:
            return None

        cur.execute(
            f"""
            SELECT {_MEMORY_COLUMNS}
              FROM memory mem
              JOIN memoir_participant p
                ON p.memoir_id = mem.memoir_id AND p.id = mem.participant_id
             WHERE mem.memoir_id = %(memoir_id)s
             ORDER BY mem.created_at DESC
            """,
            {"memoir_id": memoir_id},
        )
        return _attach_assets(cur, cur.fetchall())


def get_memory(memory_id: str, user_id: str) -> dict | None:
    """One memory in full, or None if it is not yours.

    The archive list already carries everything this returns, so the detail page
    usually renders from the query cache without touching this at all. It exists
    for the two cases the cache cannot serve: a link opened directly, and a
    refresh.

    Ownership goes through `owned_memoir_of_memory`, the same helper `PATCH` and
    `DELETE` use, so "not yours" and "does not exist" are one answer here for the
    same reason they are there.
    """
    with db() as conn, conn.cursor() as cur:
        if owned_memoir_of_memory(cur, memory_id, user_id) is None:
            return None

        cur.execute(
            f"""
            SELECT {_MEMORY_COLUMNS}
              FROM memory mem
              JOIN memoir_participant p
                ON p.memoir_id = mem.memoir_id AND p.id = mem.participant_id
             WHERE mem.id = %(memory_id)s
            """,
            {"memory_id": memory_id},
        )
        memory = cur.fetchone()
        if memory is None:
            return None

        return _attach_assets(cur, [memory])[0]


def create_memory(memoir_id: str, user_id: str, payload: dict) -> dict | None:
    """Record a memory the owner wrote themselves.

    The owner is a participant in their own memoir — the claim step created
    their `owner` row — so this attributes to that row rather than inventing a
    second identity for them.
    """
    with db() as conn, conn.cursor() as cur:
        memoir = owned_memoir(cur, memoir_id, user_id)
        if memoir is None:
            return None
        if memoir["status"] == "published":
            raise MemoirPublished

        cur.execute(
            """
            SELECT id FROM memoir_participant
             WHERE memoir_id = %(memoir_id)s AND role = 'owner'
            """,
            {"memoir_id": memoir_id},
        )
        owner = cur.fetchone()
        if owner is None:
            # A memoir with no owner row should be impossible: the claim
            # transaction creates both together, and the partial unique index
            # guarantees exactly one. Worth a loud log if it ever happens.
            logger.error("Memoir %s has no owner participant", memoir_id)
            return None

        memory = _insert_memory(cur, memoir_id, str(owner["id"]), payload)
        return _attach_assets(cur, [memory])[0]


def update_memory(memory_id: str, user_id: str, fields: dict) -> dict | None:
    """Edit a memory. None if it is not yours or does not exist.

    `fields` has already been through `exclude_unset=True`, so an absent key
    means "leave it alone" and a present `None` means "clear it". Building the
    SET clause from the keys present is what preserves that distinction.
    """
    if not fields:
        return None

    with db() as conn, conn.cursor() as cur:
        memoir = owned_memoir_of_memory(cur, memory_id, user_id)
        if memoir is None:
            return None
        if memoir["status"] == "published":
            raise MemoirPublished

        # Column names come from a fixed allow-list, never from client input —
        # they are identifiers, which cannot be passed as parameters. The
        # values still go through %(name)s placeholders.
        allowed = {"title", "body_text", "happened_on"}
        columns = [key for key in fields if key in allowed]
        if not columns:
            return None

        assignments = ", ".join(f"{column} = %({column})s" for column in columns)
        params = {column: fields[column] for column in columns}
        params["memory_id"] = memory_id

        cur.execute(
            f"""
            UPDATE memory mem
               SET {assignments}, updated_at = now()
              FROM memoir_participant p
             WHERE mem.id = %(memory_id)s
               AND p.memoir_id = mem.memoir_id
               AND p.id = mem.participant_id
         RETURNING {_MEMORY_COLUMNS}
            """,
            params,
        )
        memory = cur.fetchone()
        return _attach_assets(cur, [memory])[0] if memory else None


def delete_memory(memory_id: str, user_id: str) -> bool:
    """Remove a memory and the objects it held. False if it was not yours.

    The database row goes first, inside the transaction; the storage objects
    go afterwards, outside it. That order is deliberate. If deleting an object
    fails, the memory is still gone and the user's request succeeded, at the
    cost of an orphaned file. The reverse order risks a file deleted out from
    under a memory that is still on the page.
    """
    with db() as conn, conn.cursor() as cur:
        memoir = owned_memoir_of_memory(cur, memory_id, user_id)
        if memoir is None:
            return False
        if memoir["status"] == "published":
            raise MemoirPublished

        # Read the paths before the cascade takes the rows with it.
        cur.execute(
            "SELECT storage_path FROM media_asset WHERE memory_id = %(id)s",
            {"id": memory_id},
        )
        paths = [row["storage_path"] for row in cur.fetchall()]

        cur.execute("DELETE FROM memory WHERE id = %(id)s", {"id": memory_id})

    for path in paths:
        delete_object(path)

    return True


def storage_used_bytes(user_id: str) -> int:
    """Total confirmed bytes across every memoir this user owns.

    Counts `uploaded_at IS NOT NULL` only, so a reservation that never became a
    file is not charged to anyone. COALESCE because SUM over no rows is NULL,
    and a brand-new account should read 0, not null.
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(a.byte_size), 0) AS used
              FROM media_asset a
              JOIN memoir m ON m.id = a.memoir_id
             WHERE m.created_by_user_id = %(user_id)s
               AND a.uploaded_at IS NOT NULL
            """,
            {"user_id": user_id},
        )
        return int(cur.fetchone()["used"])


# ---------------------------------------------------------------------------
# The contributor's side
# ---------------------------------------------------------------------------


def contribute_memory(link_token: str, payload: dict) -> dict | None:
    """Record a memory from someone with no account.

    Returns the memory plus the participant token that identifies them next
    time, or None if the link is dead, revoked, view-only, or the memoir has
    been published.

    The whole transaction is one unit: if the memory insert fails, the
    participant that was created for it is rolled back too, rather than leaving
    a name in the contributors list belonging to someone who never managed to
    contribute anything.
    """
    with db() as conn, conn.cursor() as cur:
        memoir = contributable_memoir(cur, link_token)
        if memoir is None:
            return None

        memoir_id = str(memoir["id"])
        participant = _resolve_contributor(
            cur,
            memoir_id=memoir_id,
            token=payload.get("participant_token"),
            display_name=payload["display_name"],
        )

        memory = _insert_memory(cur, memoir_id, str(participant["id"]), payload)
        memory = _attach_assets(cur, [memory])[0]

        return {
            "memory": memory,
            "participant_token": participant["contributor_token"],
        }


def _resolve_contributor(
    cur, memoir_id: str, token: str | None, display_name: str
) -> dict:
    """Find the returning contributor, or create a new one.

    The token is matched together with `memoir_id`, which is what stops a token
    earned on one memoir being used to post into another. A token that does not
    match is not an error — it is a cleared cookie or a different phone — so it
    falls through to creating a new participant rather than rejecting the
    contribution. Losing a name is recoverable; losing the memory is not.

    `first_opened_at` is stamped here because arriving with something to say is
    the strongest evidence there is that the link was opened.
    """
    if token:
        cur.execute(
            """
            SELECT id, contributor_token
              FROM memoir_participant
             WHERE memoir_id = %(memoir_id)s
               AND contributor_token = %(token)s
               AND role = 'contributor'
            """,
            {"memoir_id": memoir_id, "token": token},
        )
        existing = cur.fetchone()
        if existing is not None:
            return existing

        logger.info("Contributor token did not match; treating as a new person")

    cur.execute(
        """
        INSERT INTO memoir_participant
            (memoir_id, role, display_name, relationship,
             first_opened_at, contributor_token)
        VALUES
            (%(memoir_id)s, 'contributor', %(display_name)s, 'other',
             now(), encode(gen_random_bytes(24), 'hex'))
        RETURNING id, contributor_token
        """,
        {"memoir_id": memoir_id, "display_name": display_name.strip()},
    )
    return cur.fetchone()


def list_contributions(link_token: str, participant_token: str) -> list[dict] | None:
    """What one contributor has added, for their own review.

    Scoped to their participant row and nothing else. A contributor may see
    what they wrote and must never see the archive or anyone else's memories —
    the contributor screen promises exactly that, and this query is where the
    promise is kept.
    """
    with db() as conn, conn.cursor() as cur:
        memoir = contributable_memoir(cur, link_token)
        if memoir is None:
            return None

        cur.execute(
            f"""
            SELECT {_MEMORY_COLUMNS}
              FROM memory mem
              JOIN memoir_participant p
                ON p.memoir_id = mem.memoir_id AND p.id = mem.participant_id
             WHERE mem.memoir_id = %(memoir_id)s
               AND p.contributor_token = %(token)s
             ORDER BY mem.created_at DESC
            """,
            {"memoir_id": str(memoir["id"]), "token": participant_token},
        )
        return _attach_assets(cur, cur.fetchall())
