# Domain layer for the finished memoir — reading it, and talking about it.
#
# Nothing here imports fastapi. These functions take plain values, return plain
# dicts, and signal failure by returning None or raising a domain exception.
# Deciding that None means 404 lives in src/api/chapters.py.
#
# ---------------------------------------------------------------------------
# Two audiences, one set of queries
# ---------------------------------------------------------------------------
# Every read here is reachable two ways: by the owner with a bearer token, or by
# anyone holding a live **view** link. Same rows, same shape, different proof —
# the same posture `media/` already takes for uploads. The alternative was a
# second parallel set of endpoints for the public side, which is how the two
# drift and how one of them eventually returns a field the other filters out.
#
# ---------------------------------------------------------------------------
# Nothing in this file writes a chapter
# ---------------------------------------------------------------------------
# Assembly — the Claude call that reads the whole archive and produces blocks
# with attributed spans — is a later slice. Chapters arrive by SQL until then.
# What this file settles is the shape that step has to emit, which is the part
# that gets expensive to change once a prompt is written against it.

import logging

from src.domain.memoirs.access import owned_memoir, readable_memoir
from src.domain.memories.memory_service import resolve_participant
from src.integrations.db import db
from src.integrations.supabase_storage import create_signed_download_urls

logger = logging.getLogger(__name__)


class NameRequired(Exception):
    """A reader tried to comment without saying who they are.

    Only reachable on the link path. The owner is already named by their token;
    somebody arriving by link has nothing else identifying them, and an
    unattributed comment in a memoir is worse than no comment — the whole
    product is about who said what.
    """


class SpanOutOfRange(Exception):
    """A comment anchored past the end of the paragraph it points at.

    Checked here because the database cannot: `thread_span_is_whole_or_real`
    knows the offsets are ordered and non-negative, but not how long the block's
    text is. An offset past the end would store fine and then silently
    highlight nothing forever, and a published memoir cannot be corrected.
    """


# ---------------------------------------------------------------------------
# Reaching a chapter
# ---------------------------------------------------------------------------


def _reachable_chapter(
    cur, chapter_id: str, user_id: str | None, link_token: str | None
) -> dict | None:
    """The chapter, if this caller may read it. None otherwise.

    Owner first, then view link — the caller may legitimately hold both, and
    trying the stronger credential first means an owner reading their own
    unpublished memoir does not depend on a link existing.

    Both branches join back up to `memoir`, so a chapter id from somebody
    else's memoir matches nothing rather than matching on the id alone.
    """
    if user_id is not None:
        cur.execute(
            """
            SELECT c.id, c.memoir_id, c.ordinal, c.title,
                   c.from_year, c.through_year
              FROM chapter c
              JOIN memoir m ON m.id = c.memoir_id
             WHERE c.id = %(chapter)s
               AND m.created_by_user_id = %(user)s
            """,
            {"chapter": chapter_id, "user": user_id},
        )
        row = cur.fetchone()
        if row is not None:
            return row

    if link_token is not None:
        cur.execute(
            """
            SELECT c.id, c.memoir_id, c.ordinal, c.title,
                   c.from_year, c.through_year
              FROM chapter c
              JOIN memoir_link l ON l.memoir_id = c.memoir_id
             WHERE c.id = %(chapter)s
               AND l.token = %(token)s
               AND l.revoked_at IS NULL
               AND l.scope = 'view'
            """,
            {"chapter": chapter_id, "token": link_token},
        )
        row = cur.fetchone()
        if row is not None:
            return row

    return None


# ---------------------------------------------------------------------------
# The covers: contents, people, colophon
# ---------------------------------------------------------------------------


def _chapter_summaries(cur, memoir_id: str) -> list[dict]:
    cur.execute(
        """
        SELECT id, ordinal, title, from_year, through_year
          FROM chapter
         WHERE memoir_id = %(memoir)s
         ORDER BY ordinal
        """,
        {"memoir": memoir_id},
    )
    return cur.fetchall()


def _people(cur, memoir_id: str) -> list[dict]:
    """Everyone who left something, most first.

    Only participants who actually contributed. Somebody who opened the link
    and never sent anything is a real row in `memoir_participant` and belongs on
    the owner's contributors screen — but the back matter of the book is an
    index of the people in it, and listing a name with nothing beside it reads
    as an accusation.

    `merged_into IS NULL` skips the losing half of a merge, whose memories the
    query already counts under the winner.
    """
    cur.execute(
        """
        SELECT p.id AS participant_id,
               p.display_name AS name,
               p.relationship::text AS relationship,
               COUNT(mem.id)::int AS memory_count
          FROM memoir_participant p
          LEFT JOIN memory mem
                 ON mem.memoir_id = p.memoir_id
                AND mem.participant_id = p.id
         WHERE p.memoir_id = %(memoir)s
           AND p.merged_into IS NULL
         GROUP BY p.id, p.display_name, p.relationship
        HAVING COUNT(mem.id) > 0
         ORDER BY COUNT(mem.id) DESC, p.display_name
        """,
        {"memoir": memoir_id},
    )
    return cur.fetchall()


def _totals(cur, memoir_id: str) -> dict:
    """The colophon's four numbers.

    `people` counts those who contributed, not everyone invited, so it agrees
    with the list `_people` returns. Two numbers on one page that are both
    called "people" and disagree is the kind of thing a family notices and
    never trusts again.
    """
    cur.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM memory
            WHERE memoir_id = %(memoir)s)::int AS memories,
          (SELECT COUNT(DISTINCT participant_id) FROM memory
            WHERE memoir_id = %(memoir)s)::int AS people,
          (SELECT COUNT(*) FROM chapter
            WHERE memoir_id = %(memoir)s)::int AS chapters,
          (SELECT COUNT(*) FROM media_asset
            WHERE memoir_id = %(memoir)s
              AND kind = 'audio'
              AND uploaded_at IS NOT NULL)::int AS recordings
        """,
        {"memoir": memoir_id},
    )
    return cur.fetchone()


def _reading(cur, memoir: dict) -> dict:
    """Assemble the covers from a memoir row that has already been authorized."""
    memoir_id = str(memoir["id"])
    return {
        "memoir_id": memoir["id"],
        "subject_name": memoir["subject_name"],
        "born_year": memoir["born_year"],
        "through_year": memoir["through_year"],
        "subject_is_living": memoir["subject_is_living"],
        "published_at": memoir["published_at"],
        "chapters": _chapter_summaries(cur, memoir_id),
        "people": _people(cur, memoir_id),
        "totals": _totals(cur, memoir_id),
    }


def reading_for_owner(memoir_id: str, user_id: str) -> dict | None:
    """The covers, for the owner reading their own memoir. None if not theirs."""
    with db() as conn, conn.cursor() as cur:
        owned = owned_memoir(cur, memoir_id, user_id)
        if owned is None:
            return None

        # `owned_memoir` returns only what a write path needs. The covers need
        # the dates and the publication state too, so read the row properly
        # rather than widening a helper six other callers depend on.
        cur.execute(
            """
            SELECT id, subject_name, born_year, through_year,
                   subject_is_living, published_at
              FROM memoir
             WHERE id = %(memoir)s
            """,
            {"memoir": memoir_id},
        )
        return _reading(cur, cur.fetchone())


def reading_for_link(link_token: str) -> dict | None:
    """The covers, for anybody holding a live view link. None if it is dead."""
    with db() as conn, conn.cursor() as cur:
        memoir = readable_memoir(cur, link_token)
        if memoir is None:
            return None
        return _reading(cur, memoir)


# ---------------------------------------------------------------------------
# One chapter in full
# ---------------------------------------------------------------------------


def _figures(cur, memoir_id: str, blocks: list[dict]) -> dict:
    """`{asset_id: figure}` for every figure block, with signed URLs.

    Two round trips regardless of how many photographs a chapter holds: one
    query for all of them, one batch call to storage to sign them.

    The caption is read from the **memory** the photograph came from, never
    written here. It is what the person who gave it said about it, and
    inventing a description of somebody's photograph is exactly what "never
    fabricate" forbids. `LEFT JOIN` because an asset that was never adopted by
    a memory has no caption and no giver — it renders bare rather than 500ing.
    """
    asset_ids = [b["asset_id"] for b in blocks if b["kind"] == "figure"]
    if not asset_ids:
        return {}

    cur.execute(
        """
        SELECT a.id AS asset_id,
               a.storage_path,
               mem.title       AS memory_title,
               mem.body_text   AS memory_body,
               COALESCE(
                   EXTRACT(YEAR FROM mem.happened_on),
                   EXTRACT(YEAR FROM mem.created_at)
               )::int          AS year,
               p.id            AS credit_participant_id,
               p.display_name  AS credit
          FROM media_asset a
          LEFT JOIN memory mem
                 ON mem.memoir_id = a.memoir_id AND mem.id = a.memory_id
          LEFT JOIN memoir_participant p
                 ON p.memoir_id = mem.memoir_id AND p.id = mem.participant_id
         WHERE a.memoir_id = %(memoir)s
           AND a.id = ANY(%(ids)s)
        """,
        {"memoir": memoir_id, "ids": asset_ids},
    )
    rows = cur.fetchall()

    signed = create_signed_download_urls([r["storage_path"] for r in rows])

    figures = {}
    for row in rows:
        # The title if they gave one, otherwise whatever they typed under the
        # photograph. Both are the contributor's own words.
        title = (row["memory_title"] or "").strip()
        body = (row["memory_body"] or "").strip()

        figures[row["asset_id"]] = {
            "asset_id": row["asset_id"],
            "url": signed.get(row["storage_path"]),
            "caption": title or body or None,
            "credit": row["credit"],
            "credit_participant_id": row["credit_participant_id"],
            "year": row["year"],
        }
    return figures


def _sources(cur, memoir_id: str, block_ids: list[str]) -> dict:
    """`{block_id: [source, ...]}` for a whole chapter, in one query.

    `duration_ms` is the longest recording on the memory, so a voice credit can
    print "0:47". A correlated subquery rather than a join, because a memory
    with three recordings must not multiply its source row into three.

    Ordered by where the span starts, so the margin renders credits in the
    order the eye meets the words they belong to. Whole-block sources sort
    first — they are about the paragraph, not a phrase inside it.
    """
    if not block_ids:
        return {}

    cur.execute(
        """
        SELECT s.id,
               s.block_id,
               s.memory_id,
               s.participant_id,
               s.start_offset,
               s.end_offset,
               s.diverges,
               p.display_name        AS name,
               p.relationship::text  AS relationship,
               mem.kind::text        AS medium,
               COALESCE(
                   EXTRACT(YEAR FROM mem.happened_on),
                   EXTRACT(YEAR FROM mem.created_at)
               )::int                AS year,
               (SELECT MAX(a.duration_ms)
                  FROM media_asset a
                 WHERE a.memoir_id = s.memoir_id
                   AND a.memory_id = s.memory_id
                   AND a.kind = 'audio')  AS duration_ms
          FROM block_source s
          JOIN memoir_participant p
            ON p.memoir_id = s.memoir_id AND p.id = s.participant_id
          JOIN memory mem
            ON mem.memoir_id = s.memoir_id AND mem.id = s.memory_id
         WHERE s.memoir_id = %(memoir)s
           AND s.block_id = ANY(%(ids)s)
         ORDER BY s.start_offset NULLS FIRST, p.display_name
        """,
        {"memoir": memoir_id, "ids": block_ids},
    )

    by_block: dict = {}
    for row in cur.fetchall():
        by_block.setdefault(row.pop("block_id"), []).append(row)
    return by_block


def _threads(cur, chapter_id: str) -> list[dict]:
    """Every conversation in the chapter, each with its comments oldest first."""
    cur.execute(
        """
        SELECT id, chapter_id, block_id, start_offset, end_offset, resolved_at
          FROM comment_thread
         WHERE chapter_id = %(chapter)s
         ORDER BY created_at
        """,
        {"chapter": chapter_id},
    )
    threads = cur.fetchall()
    if not threads:
        return []

    cur.execute(
        """
        SELECT c.id,
               c.thread_id,
               c.participant_id,
               c.body,
               c.created_at,
               p.display_name       AS name,
               p.relationship::text AS relationship,
               (p.role = 'owner')   AS is_owner
          FROM comment c
          JOIN memoir_participant p
            ON p.memoir_id = c.memoir_id AND p.id = c.participant_id
         WHERE c.thread_id = ANY(%(ids)s)
         ORDER BY c.created_at
        """,
        {"ids": [t["id"] for t in threads]},
    )

    by_thread: dict = {}
    for row in cur.fetchall():
        by_thread.setdefault(row.pop("thread_id"), []).append(row)

    for thread in threads:
        thread["comments"] = by_thread.get(thread["id"], [])

    # A thread whose every comment was somehow lost would render as an empty
    # card. Nothing deletes comments today, so this cannot happen — but the
    # reader should never be asked to draw a conversation with nothing in it.
    return [t for t in threads if t["comments"]]


def _told_by(sources_by_block: dict) -> tuple[list[str], int]:
    """Who this chapter came from, most-cited first, and how many memories.

    Counted from the sources rather than from a column, so the closing line of
    a chapter cannot disagree with the credits in its own margin.
    """
    weight: dict[str, int] = {}
    memories: set = set()
    for sources in sources_by_block.values():
        for source in sources:
            weight[source["name"]] = weight.get(source["name"], 0) + 1
            memories.add(source["memory_id"])

    ordered = sorted(weight.items(), key=lambda pair: (-pair[1], pair[0]))
    return [name for name, _ in ordered], len(memories)


def get_chapter(
    chapter_id: str, *, user_id: str | None = None, link_token: str | None = None
) -> dict | None:
    """One chapter, with its photographs, its sources and its conversation."""
    with db() as conn, conn.cursor() as cur:
        chapter = _reachable_chapter(cur, chapter_id, user_id, link_token)
        if chapter is None:
            return None

        memoir_id = str(chapter["memoir_id"])

        cur.execute(
            """
            SELECT id, ordinal, kind::text AS kind, text,
                   asset_id, placement::text AS placement, anchor_block_id
              FROM chapter_block
             WHERE memoir_id = %(memoir)s
               AND chapter_id = %(chapter)s
             ORDER BY ordinal
            """,
            {"memoir": memoir_id, "chapter": chapter_id},
        )
        rows = cur.fetchall()

        figures = _figures(cur, memoir_id, rows)
        sources = _sources(cur, memoir_id, [r["id"] for r in rows])

        blocks = []
        for row in rows:
            figure = None
            if row["kind"] == "figure":
                # Present by construction — `block_figure_shape` requires it —
                # but a figure whose asset row vanished renders as prose-less
                # rather than as a KeyError on somebody's grandmother's page.
                base = figures.get(row["asset_id"])
                if base is not None:
                    figure = {
                        **base,
                        "placement": row["placement"],
                        "anchor_block_id": row["anchor_block_id"],
                    }

            blocks.append(
                {
                    "id": row["id"],
                    "ordinal": row["ordinal"],
                    "kind": row["kind"],
                    "text": row["text"],
                    "figure": figure,
                    "sources": sources.get(row["id"], []),
                }
            )

        told_by, memory_count = _told_by(sources)

        return {
            **chapter,
            "blocks": blocks,
            "threads": _threads(cur, chapter_id),
            "told_by": told_by,
            "memory_count": memory_count,
        }


def list_threads(
    chapter_id: str, *, user_id: str | None = None, link_token: str | None = None
) -> list[dict] | None:
    """Just the conversation.

    `get_chapter` already returns it, so this exists for the one thing that
    changes after the page is drawn: somebody else commenting. The reader polls
    this rather than re-fetching a chapter's whole prose and re-signing every
    photograph to find out.
    """
    with db() as conn, conn.cursor() as cur:
        if _reachable_chapter(cur, chapter_id, user_id, link_token) is None:
            return None
        return _threads(cur, chapter_id)


# ---------------------------------------------------------------------------
# The layer that stays open
# ---------------------------------------------------------------------------


def _owner_participant(cur, memoir_id: str) -> dict:
    cur.execute(
        """
        SELECT id FROM memoir_participant
         WHERE memoir_id = %(memoir)s AND role = 'owner'
        """,
        {"memoir": memoir_id},
    )
    return cur.fetchone()


def _one_thread(cur, thread_id: str) -> dict | None:
    cur.execute(
        """
        SELECT id, chapter_id, block_id, start_offset, end_offset, resolved_at
          FROM comment_thread
         WHERE id = %(thread)s
        """,
        {"thread": thread_id},
    )
    thread = cur.fetchone()
    if thread is None:
        return None

    cur.execute(
        """
        SELECT c.id, c.participant_id, c.body, c.created_at,
               p.display_name       AS name,
               p.relationship::text AS relationship,
               (p.role = 'owner')   AS is_owner
          FROM comment c
          JOIN memoir_participant p
            ON p.memoir_id = c.memoir_id AND p.id = c.participant_id
         WHERE c.thread_id = %(thread)s
         ORDER BY c.created_at
        """,
        {"thread": thread_id},
    )
    thread["comments"] = cur.fetchall()
    return thread


def add_comment(
    chapter_id: str,
    payload: dict,
    *,
    user_id: str | None = None,
    link_token: str | None = None,
) -> dict | None:
    """Leave a comment, starting a thread or replying to one.

    Returns the thread as it now stands plus the token that makes this person
    the same person next time, or None if the chapter cannot be reached.

    One transaction throughout. A thread created and then failing to receive
    its first comment would be an empty conversation nobody can delete, in a
    memoir nobody can edit.

    Note what is deliberately *not* checked: whether the memoir is published.
    Every other write in this codebase refuses a published memoir, and this is
    the one exception the product asks for by name — "the comment layer stays
    open… for as long as they want". Commenting before publication is allowed
    too, so an owner can send a view link to one person and hear back.
    """
    with db() as conn, conn.cursor() as cur:
        chapter = _reachable_chapter(cur, chapter_id, user_id, link_token)
        if chapter is None:
            return None

        memoir_id = str(chapter["memoir_id"])

        # --- who is talking ------------------------------------------------
        if user_id is not None:
            participant = _owner_participant(cur, memoir_id)
            participant_token = None
        else:
            display_name = (payload.get("display_name") or "").strip()
            if not display_name:
                raise NameRequired
            participant = resolve_participant(
                cur,
                memoir_id=memoir_id,
                token=payload.get("participant_token"),
                display_name=display_name,
            )
            participant_token = participant["contributor_token"]

        # --- which conversation --------------------------------------------
        thread_id = payload.get("thread_id")
        if thread_id is not None:
            cur.execute(
                """
                SELECT id FROM comment_thread
                 WHERE id = %(thread)s AND chapter_id = %(chapter)s
                """,
                {"thread": str(thread_id), "chapter": chapter_id},
            )
            if cur.fetchone() is None:
                # A thread id from another chapter looks exactly like one that
                # does not exist, same as everywhere else in this API.
                return None
            thread_id = str(thread_id)
        else:
            thread_id = _open_thread(cur, chapter_id, memoir_id, payload)
            if thread_id is None:
                return None

        cur.execute(
            """
            INSERT INTO comment (memoir_id, thread_id, participant_id, body)
            VALUES (%(memoir)s, %(thread)s, %(participant)s, %(body)s)
            """,
            {
                "memoir": memoir_id,
                "thread": thread_id,
                "participant": str(participant["id"]),
                "body": payload["body"].strip(),
            },
        )

        return {
            "thread": _one_thread(cur, thread_id),
            "participant_token": participant_token,
        }


def _open_thread(cur, chapter_id: str, memoir_id: str, payload: dict) -> str | None:
    """Start a conversation about a block, or a range of characters inside it.

    The block is re-read here rather than trusted from the request, for two
    reasons: a block id belonging to another chapter must not be commentable
    through this one, and the offsets have to be checked against the real
    length of the real text. The database can only tell that `end > start`.
    """
    cur.execute(
        """
        SELECT id, char_length(coalesce(text, '')) AS length
          FROM chapter_block
         WHERE id = %(block)s
           AND chapter_id = %(chapter)s
           AND memoir_id = %(memoir)s
        """,
        {"block": str(payload["block_id"]), "chapter": chapter_id, "memoir": memoir_id},
    )
    block = cur.fetchone()
    if block is None:
        return None

    start = payload.get("start_offset")
    end = payload.get("end_offset")
    if end is not None and end > block["length"]:
        raise SpanOutOfRange

    cur.execute(
        """
        INSERT INTO comment_thread
            (memoir_id, chapter_id, block_id, start_offset, end_offset)
        VALUES (%(memoir)s, %(chapter)s, %(block)s, %(start)s, %(end)s)
        RETURNING id
        """,
        {
            "memoir": memoir_id,
            "chapter": chapter_id,
            "block": str(block["id"]),
            "start": start,
            "end": end,
        },
    )
    return str(cur.fetchone()["id"])
