# The assembled page, as its owner corrects it by hand.
#
# Nothing here imports fastapi. It returns plain dicts, returns None for "not
# yours", and raises its own exceptions; api/chapters.py decides the codes.
#
# ---------------------------------------------------------------------------
# Why this is a third file and not a branch in one of the other two
# ---------------------------------------------------------------------------
# `chapter_service.py` reads the book and says so in its header. `assembly_
# service.py` writes it *from the archive* — a model's decisions, or the decade
# fallback's, turned into rows. This is neither: it is one person changing the
# words of their own book, with no archive and no model involved.
#
# It matters because the two have opposite failure modes. Assembly may throw
# away and rewrite everything, because it is rebuilding from the source of
# truth. A hand edit may not: what it touches is already the only copy of a
# sentence the owner wrote, and every span pointing into it has to survive.
#
# ---------------------------------------------------------------------------
# The rule this file exists to keep
# ---------------------------------------------------------------------------
# `block_source.start_offset` / `end_offset` are character positions into a
# block's text. Edit that text and every offset past the edit points somewhere
# else — at the middle of a word, or at a clause a different person said. So
# every edited passage is re-surveyed: each span is *re-found* by the exact
# substring it used to cover, and falls back to whole-block attribution when
# the words are gone or now appear twice. `plan_service.resurvey` is that rule,
# written once and reused here rather than copied — the plan and the page have
# to agree about it or attribution means two different things depending on
# which screen the owner used.
#
# `comment_thread` carries the same pair of offsets and gets the same
# treatment. It is normally empty at this point — reading needs a view link and
# a view link needs publication — but an owner can leave a comment on their own
# unpublished chapter, and a comment silently pointing at moved words is the
# exact failure the offsets were checked to avoid.
#
# ---------------------------------------------------------------------------
# What an edit may not do
# ---------------------------------------------------------------------------
# It cannot add a paragraph, and it cannot move one to another chapter. A block
# with no `block_source` behind it is a fabricated one, and this file has no
# archive to attribute new prose to; moving prose between chapters is what the
# plan is for, where the chapter it belongs in is still a decision rather than
# a row. It also cannot touch a published memoir, for the reason
# `assembly_service.MemoirSealed` gives.

import logging

from src.domain.chapters.assembly_service import MemoirSealed
from src.domain.chapters.plan_service import resurvey
from src.integrations.db import db

logger = logging.getLogger(__name__)

# Ordinals are renumbered in two passes because `UNIQUE (chapter_id, ordinal)`
# would fire halfway through a reorder — moving block 3 to position 1 collides
# with whatever is already first. The parking offset is added, then removed.
# `ordinal` is a smallint and `block_ordinal_positive` refuses a negative, so
# it parks high rather than below zero.
_PARK = 1000


class UnknownBlock(Exception):
    """The submitted page names a block this chapter does not hold.

    Leaving a block out of the list is how the owner removes it. Adding one is
    refused: prose with nothing behind it in the archive is a fabricated
    paragraph, and this file cannot attribute what it did not read.
    """


class EmptyPassage(Exception):
    """A passage was submitted blank.

    `block_prose_shape` refuses empty prose, and coercing it to a space would
    store a paragraph that reads as a printing error. Removing a passage is a
    separate, explicit act — leave it out of the list.
    """


class PageEmptied(Exception):
    """The edit removed every passage from the chapter.

    A chapter with no prose cannot hold its photographs either — a figure needs
    a paragraph to anchor to — so this would quietly take the pictures with it.
    Refused, and the owner can leave the chapter out of the plan instead, which
    is where removing a whole chapter belongs.
    """


class BadAnchor(Exception):
    """A photograph was pointed at something it cannot sit beside.

    A figure anchors to a **paragraph in this chapter that survives the edit**.
    Never to a pulled line, whose one lifted sentence would caption a
    photograph with a fragment; never to a block being removed, which would
    take the photograph with it through the cascade.
    """


async def _owned_chapter(cur, chapter_id: str, user_id: str) -> dict | None:
    """The chapter with its memoir's publication state, if it is this user's.

    Joined back to `memoir` rather than trusting the chapter id, so a chapter
    from somebody else's memoir matches nothing rather than matching on the id
    alone — the same shape `_reachable_chapter` uses, and the reason this
    answers 404 and never 403.
    """
    await cur.execute(
        """
        SELECT c.id, c.memoir_id, m.status::text AS status
          FROM chapter c
          JOIN memoir m ON m.id = c.memoir_id
         WHERE c.id = %(chapter)s
           AND m.created_by_user_id = %(user)s
        """,
        {"chapter": chapter_id, "user": user_id},
    )
    return await cur.fetchone()


async def _blocks(cur, chapter_id: str) -> list[dict]:
    """Every block of the chapter as it stands, in reading order."""
    await cur.execute(
        """
        SELECT id, ordinal, kind::text AS kind, text,
               asset_id, placement::text AS placement, anchor_block_id
          FROM chapter_block
         WHERE chapter_id = %(chapter)s
         ORDER BY ordinal
        """,
        {"chapter": chapter_id},
    )
    return await cur.fetchall()


# The two tables that hold a span into a block's text, and the pair of
# statements each needs. Written out rather than built from a table name: the
# security tier forbids interpolating anything into SQL — see
# `tests/static/test_secrets.py` — and a rule with one more exception in it is
# a weaker rule than four literal strings.
_SPANS = (
    (
        """
        SELECT id, start_offset, end_offset
          FROM block_source
         WHERE block_id = %(block)s
        """,
        """
        UPDATE block_source
           SET start_offset = %(start)s, end_offset = %(end)s
         WHERE id = %(id)s
        """,
    ),
    (
        """
        SELECT id, start_offset, end_offset
          FROM comment_thread
         WHERE block_id = %(block)s
        """,
        """
        UPDATE comment_thread
           SET start_offset = %(start)s, end_offset = %(end)s
         WHERE id = %(id)s
        """,
    ),
)


async def _resurvey_spans(cur, block_id: str, old: str, new: str) -> None:
    """Re-place every span pointing into one edited block.

    Both tables get the same treatment because the same two columns mean the
    same thing in both: character offsets into this block's text. A source
    whose words moved loses its precision and keeps its credit; a comment whose
    words moved comes to rest on the whole paragraph instead of on a clause it
    no longer describes.
    """
    for select, update in _SPANS:
        await cur.execute(select, {"block": block_id})
        spans = await cur.fetchall()

        for moved in resurvey(old, new, spans):
            await cur.execute(
                update,
                {
                    "id": moved["id"],
                    "start": moved["start_offset"],
                    "end": moved["end_offset"],
                },
            )


def _plan_page(current: list[dict], submitted: list[dict]) -> list[dict]:
    """What the chapter should hold, in order. Raises rather than guessing.

    Pure, and separated from the writing so the rules can be read in one place:

      order        array position, renumbered from zero server-side. A client
                   ordinal is never trusted — it is the one number two open
                   tabs are guaranteed to disagree about.
      membership   a block left out is removed. A block that was never there
                   is refused.
      text         prose and pulled lines only, and never blank.
      figures      placement and anchor may both move; the anchor has to be a
                   paragraph that is still here afterwards.
    """
    by_id = {str(block["id"]): block for block in current}

    ids = [str(block["id"]) for block in submitted]
    if len(set(ids)) != len(ids):
        raise UnknownBlock("a passage was listed twice")
    unknown = [block_id for block_id in ids if block_id not in by_id]
    if unknown:
        raise UnknownBlock("no such passage in this chapter")

    # Which anchors will still exist. Computed before anything is written,
    # because the alternative is discovering it as a cascade that silently ate
    # a photograph the owner was only moving.
    surviving_paragraphs = {
        block_id for block_id in ids if by_id[block_id]["kind"] == "paragraph"
    }
    if not surviving_paragraphs:
        raise PageEmptied

    page: list[dict] = []

    for ordinal, block in enumerate(submitted):
        stored = by_id[str(block["id"])]
        kept = {**stored, "ordinal": ordinal}

        if stored["kind"] == "figure":
            placement = block.get("placement") or stored["placement"]
            anchor = str(block.get("anchor_block_id") or stored["anchor_block_id"])
            if anchor not in surviving_paragraphs:
                raise BadAnchor("a photograph can only sit beside a paragraph")
            kept["placement"] = placement
            kept["anchor_block_id"] = anchor
        else:
            text = block.get("text")
            if text is not None:
                if not text.strip():
                    raise EmptyPassage(
                        "a passage cannot be empty — leave it out instead"
                    )
                kept["text"] = text

        page.append(kept)

    return page


async def edit_chapter(
    chapter_id: str,
    user_id: str,
    *,
    title: str | None = None,
    blocks: list[dict] | None = None,
) -> bool:
    """Apply the owner's corrections to one assembled chapter.

    False if the chapter is not theirs — the caller answers 404, because this
    API never confirms a stranger's memoir exists. Raises `MemoirSealed`,
    `UnknownBlock`, `EmptyPassage`, `PageEmptied` or `BadAnchor`.

    One transaction. A page half-renumbered is a page with two third
    paragraphs, which `UNIQUE (chapter_id, ordinal)` would refuse anyway —
    better that the whole edit fails than that half of it lands.

    `blocks` omitted leaves the page alone, so renaming a chapter costs one
    UPDATE and touches no offsets. A blank `title` is ignored rather than
    stored: `chapter_title_not_blank` would refuse it, and an owner who cleared
    the field meant to type something.
    """
    async with db() as conn, conn.cursor() as cur:
        chapter = await _owned_chapter(cur, chapter_id, user_id)
        if chapter is None:
            return False

        if chapter["status"] == "published":
            raise MemoirSealed

        if title is not None and title.strip():
            await cur.execute(
                """
                UPDATE chapter
                   SET title = %(title)s, updated_at = now()
                 WHERE id = %(chapter)s
                """,
                {"chapter": chapter_id, "title": title.strip()},
            )

        if blocks is None:
            return True

        current = await _blocks(cur, chapter_id)
        page = _plan_page(current, blocks)
        stored_by_id = {str(block["id"]): block for block in current}

        removed = {str(block["id"]) for block in current} - {
            str(block["id"]) for block in page
        }
        if removed:
            # Cascades take the block's sources and any comment on it. A
            # removed paragraph also takes the photographs anchored to it —
            # `_plan_page` has already refused any that the owner meant to
            # keep, so what goes here is what they left out.
            await cur.execute(
                "DELETE FROM chapter_block WHERE id = ANY(%(ids)s)",
                {"ids": list(removed)},
            )
            logger.info(
                "Chapter %s: the owner removed %d block(s) by hand",
                chapter_id,
                len(removed),
            )

        # Park the ordinals, then land them. See `_PARK`.
        await cur.execute(
            """
            UPDATE chapter_block
               SET ordinal = ordinal + %(park)s
             WHERE chapter_id = %(chapter)s
            """,
            {"chapter": chapter_id, "park": _PARK},
        )

        for block in page:
            stored = stored_by_id[str(block["id"])]

            if block["kind"] == "figure":
                await cur.execute(
                    """
                    UPDATE chapter_block
                       SET ordinal         = %(ordinal)s,
                           placement       = %(placement)s::figure_placement,
                           anchor_block_id = %(anchor)s
                     WHERE id = %(id)s
                    """,
                    {
                        "id": block["id"],
                        "ordinal": block["ordinal"],
                        "placement": block["placement"],
                        "anchor": block["anchor_block_id"],
                    },
                )
                continue

            await cur.execute(
                """
                UPDATE chapter_block
                   SET ordinal = %(ordinal)s, text = %(text)s
                 WHERE id = %(id)s
                """,
                {
                    "id": block["id"],
                    "ordinal": block["ordinal"],
                    "text": block["text"],
                },
            )

            # Only when the words actually moved. Reordering a page or moving a
            # photograph does not touch a character of prose, and re-surveying
            # unchanged text can only lose precision it did not have to.
            if block["text"] != stored["text"]:
                await _resurvey_spans(
                    cur, str(block["id"]), stored["text"], block["text"]
                )

        await cur.execute(
            "UPDATE chapter SET updated_at = now() WHERE id = %(chapter)s",
            {"chapter": chapter_id},
        )

        return True
