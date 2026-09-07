# Domain layer for assembly — turning an archive into a book.
#
# Nothing here imports fastapi. It returns plain dicts and raises its own
# exceptions; api/chapters.py decides which status code each one is.
#
# ---------------------------------------------------------------------------
# This is the deterministic assembler, and it is deliberately not the final one
# ---------------------------------------------------------------------------
# Migration 0011 and `chapter_service.py` both describe assembly as "the Claude
# call that reads the whole archive and produces blocks with attributed spans".
# That call is not built. What is built is everything either side of it, and
# without *something* writing chapters the memoir is unreachable: no reader, no
# export, no search over it.
#
# So this file does the same job by rule instead of by judgement. It groups
# memories into chapters by decade, gives each memory its own block, and
# attributes that block to the person who left it — whole, with NULL offsets.
#
# The interesting consequence is that this version cannot fabricate anything at
# all. It never rephrases, so there is no assembled sentence that could drift
# from what somebody actually said, and no character offset that could point at
# the wrong clause. Every word a reader sees is a word a contributor typed or
# spoke. That is a weaker book than the Claude call will produce and a stronger
# guarantee than it can make.
#
# When the model call arrives it replaces `_plan()` below and nothing else:
# the reading, the offsets, the figure rules and the writes stay as they are.

import logging
from datetime import date

from src.domain.memoirs.access import owned_memoir
from src.integrations.db import db

logger = logging.getLogger(__name__)

# How many years one chapter covers. A decade is the unit a life divides into
# when nobody is available to say where the real seams are — and a memoir with
# four memories still wants more than one chapter, so a smaller span would make
# chapters that hold one paragraph each.
CHAPTER_SPAN_YEARS = 10

# Where memories nobody could date end up. Last, and named for what they are:
# the alternative is guessing a year, which is the one thing this file will not
# do.
UNDATED_TITLE = "Without a date"


class NothingToAssemble(Exception):
    """The archive holds nothing a chapter could be made of.

    Distinct from "no such memoir": the memoir is real and it is yours, it is
    just empty. The owner needs to be told to add memories, not that their
    memoir is missing.
    """


class MemoirSealed(Exception):
    """The memoir is published, and a published memoir cannot be reassembled.

    Not a policy preference. `block_source` and `comment_thread` store
    character offsets into block text, and migration 0011 is explicit that they
    are only safe because publication is immutable. Rewriting the blocks under
    a published memoir would leave every comment anchored to words that had
    moved, silently, forever.
    """


# ---------------------------------------------------------------------------
# Reading the archive
# ---------------------------------------------------------------------------


async def _memories(cur, memoir_id: str) -> list[dict]:
    """Every memory in the memoir, with the words and the person attached.

    One query rather than a loop over memories, because a family archive is
    hundreds of rows and this runs inside a transaction that is holding a
    connection from a bounded pool.

    `spoken` is the transcript of the longest recording on the memory, and only
    when it finished: a queued or failed transcript contributes nothing, and a
    memory whose only content is an unfinished recording is skipped rather than
    published as an empty paragraph.
    """
    await cur.execute(
        """
        SELECT m.id,
               m.participant_id,
               m.kind::text AS kind,
               m.title,
               m.body_text,
               m.happened_on,
               m.created_at,
               (SELECT t.text
                  FROM media_asset a
                  JOIN transcript t ON t.asset_id = a.id
                 WHERE a.memory_id = m.id
                   AND a.kind = 'audio'
                   AND t.status = 'done'
                 ORDER BY a.duration_ms DESC NULLS LAST
                 LIMIT 1) AS spoken,
               (SELECT a.id
                  FROM media_asset a
                 WHERE a.memory_id = m.id
                   AND a.kind = 'image'
                   AND a.uploaded_at IS NOT NULL
                 ORDER BY a.created_at
                 LIMIT 1) AS image_asset_id
          FROM memory m
         WHERE m.memoir_id = %(memoir_id)s
         ORDER BY m.happened_on NULLS LAST, m.created_at
        """,
        {"memoir_id": memoir_id},
    )
    return await cur.fetchall()


def _prose(memory: dict) -> str | None:
    """The memory's own words, or None if it has none.

    Order matters. `body_text` is what the person wrote and is preferred over
    `spoken` even on a voice memory, because someone who recorded *and* typed
    was choosing their words the second time. The title is a last resort and
    only when it is the sole thing there — a memory called "The dock" with no
    body is still something a chapter can hold.

    What this function never does is compose. It returns one field verbatim.
    """
    for candidate in (memory["body_text"], memory["spoken"], memory["title"]):
        if candidate and candidate.strip():
            return candidate.strip()
    return None


# ---------------------------------------------------------------------------
# The rule that stands in for judgement
# ---------------------------------------------------------------------------


def _band(year: int, first_year: int) -> int:
    """The start year of the decade a memory falls in, counted from the first.

    Counted from the earliest memory rather than from 1970 or from the
    subject's birth, so a memoir covering 1988–1994 gets one chapter instead of
    two split across a calendar decade boundary that means nothing to anybody.
    """
    return first_year + ((year - first_year) // CHAPTER_SPAN_YEARS) * CHAPTER_SPAN_YEARS


def _title(from_year: int, through_year: int) -> str:
    """What a chapter with no author is called.

    An en dash and two years. `chapter_title_not_blank` requires something, the
    contents rail has to print something, and inventing an evocative title
    would be writing prose nobody said — the exact thing this assembler exists
    to avoid. The owner renames these before publishing.
    """
    if from_year == through_year:
        return str(from_year)
    return f"{from_year} – {through_year}"


def _plan(memories: list[dict]) -> list[dict]:
    """Group memories into chapters. The whole of the "organising" step.

    Returns chapters in reading order, each with the memories that belong to
    it. This is the function the Claude call replaces: it is the only place
    that decides what goes where, and it decides by date alone.

    Dated memories are banded by decade. Undated ones are collected into a
    final chapter, because a memory nobody could date is not evidence that it
    happened last — it is evidence that nobody was asked.
    """
    dated: list[tuple[int, dict]] = []
    undated: list[dict] = []

    for memory in memories:
        happened: date | None = memory["happened_on"]
        if happened is None:
            undated.append(memory)
        else:
            dated.append((happened.year, memory))

    chapters: list[dict] = []

    if dated:
        first_year = dated[0][0]
        bands: dict[int, list[dict]] = {}
        for year, memory in dated:
            bands.setdefault(_band(year, first_year), []).append(memory)

        for band_start in sorted(bands):
            held = bands[band_start]
            years = [m["happened_on"].year for m in held]
            chapters.append(
                {
                    "title": _title(min(years), max(years)),
                    "from_year": min(years),
                    "through_year": max(years),
                    "memories": held,
                }
            )

    if undated:
        chapters.append(
            {
                "title": UNDATED_TITLE,
                "from_year": None,
                "through_year": None,
                "memories": undated,
            }
        )

    return chapters


# ---------------------------------------------------------------------------
# Writing it down
# ---------------------------------------------------------------------------


async def _write_chapter(cur, memoir_id: str, ordinal: int, planned: dict) -> dict:
    """One chapter and its blocks. Returns what was written, for the tally.

    Two passes over the chapter's memories, and the order is forced by the
    schema rather than chosen: `block_figure_shape` makes `anchor_block_id` NOT
    NULL on a figure, so every photograph needs a paragraph to sit beside and
    that paragraph has to exist first.

    A photograph whose chapter contains no prose at all is therefore skipped.
    It is not lost — it is still in the archive, still in the memory it came
    with — and the alternative is a figure anchored to a paragraph in a
    different part of the life, which would caption it with somebody else's
    afternoon.
    """
    await cur.execute(
        """
        INSERT INTO chapter (memoir_id, ordinal, title, from_year, through_year)
        VALUES (%(memoir_id)s, %(ordinal)s, %(title)s, %(from_year)s,
                %(through_year)s)
     RETURNING id
        """,
        {
            "memoir_id": memoir_id,
            "ordinal": ordinal,
            "title": planned["title"],
            "from_year": planned["from_year"],
            "through_year": planned["through_year"],
        },
    )
    chapter_id = (await cur.fetchone())["id"]

    next_ordinal = 0
    last_paragraph_id: str | None = None
    photographs: list[dict] = []
    written = {"blocks": 0, "sources": 0, "figures": 0}

    for memory in planned["memories"]:
        text = _prose(memory)

        if text is not None:
            await cur.execute(
                """
                INSERT INTO chapter_block (memoir_id, chapter_id, ordinal,
                                           kind, text)
                VALUES (%(memoir_id)s, %(chapter_id)s, %(ordinal)s,
                        'paragraph', %(text)s)
             RETURNING id
                """,
                {
                    "memoir_id": memoir_id,
                    "chapter_id": chapter_id,
                    "ordinal": next_ordinal,
                    "text": text,
                },
            )
            block_id = (await cur.fetchone())["id"]
            next_ordinal += 1
            last_paragraph_id = block_id
            written["blocks"] += 1

            # Whole-block attribution: NULL offsets. The paragraph IS this
            # person's words, unedited, so there is no clause of it that came
            # from anywhere else. When prose is assembled from several
            # memories these become real ranges — and every one of them will
            # have to be checked against the text before it is written.
            await cur.execute(
                """
                INSERT INTO block_source (memoir_id, block_id, memory_id,
                                          participant_id)
                VALUES (%(memoir_id)s, %(block_id)s, %(memory_id)s,
                        %(participant_id)s)
                """,
                {
                    "memoir_id": memoir_id,
                    "block_id": block_id,
                    "memory_id": memory["id"],
                    "participant_id": memory["participant_id"],
                },
            )
            written["sources"] += 1

        if memory["image_asset_id"] is not None:
            photographs.append(memory)

    for position, memory in enumerate(photographs):
        if last_paragraph_id is None:
            logger.info(
                "Chapter %s has photographs but no prose; %s not placed",
                chapter_id,
                memory["id"],
            )
            continue

        # The first photograph in a chapter runs full measure, the rest sit in
        # the margin. A rule, not a judgement — the reader supports exactly two
        # placements and something has to choose. The Claude call will choose
        # better, by looking at whether the photograph *is* the moment the
        # paragraph describes.
        placement = "inset" if position == 0 else "margin"

        await cur.execute(
            """
            INSERT INTO chapter_block (memoir_id, chapter_id, ordinal, kind,
                                       asset_id, placement, anchor_block_id)
            VALUES (%(memoir_id)s, %(chapter_id)s, %(ordinal)s, 'figure',
                    %(asset_id)s, %(placement)s::figure_placement,
                    %(anchor)s)
            """,
            {
                "memoir_id": memoir_id,
                "chapter_id": chapter_id,
                "ordinal": next_ordinal,
                "asset_id": memory["image_asset_id"],
                "placement": placement,
                "anchor": last_paragraph_id,
            },
        )
        next_ordinal += 1
        written["figures"] += 1

    return written


async def assemble(memoir_id: str, user_id: str) -> dict | None:
    """Rebuild a draft memoir's chapters from everything in its archive.

    Returns a tally of what was written, None if the memoir is not this user's.
    Raises NothingToAssemble on an empty archive and MemoirSealed on a
    published one.

    ---------------------------------------------------------------------
    Why this replaces rather than appends
    ---------------------------------------------------------------------
    Assembly is re-runnable while the memoir is a draft, because the owner will
    add memories and want the book to include them. Appending would duplicate
    every existing paragraph, so the previous chapters are deleted first and
    the whole book is rebuilt from the archive as it stands.

    The DELETE cascades to `chapter_block`, `block_source` and `comment_thread`
    — everything downstream of a chapter. Comments are the only loss that would
    matter, and a draft cannot have any: reaching the comment routes needs a
    live *view* link, and a view link is issued at publication.

    One transaction, for the reason `claim_draft` gives at length: a memoir
    left holding half of one assembly and half of another is a book with
    paragraphs missing and no code path that would ever notice.
    """
    async with db() as conn, conn.cursor() as cur:
        memoir = await owned_memoir(cur, memoir_id, user_id)
        if memoir is None:
            return None

        if memoir["status"] == "published":
            raise MemoirSealed

        memories = await _memories(cur, memoir_id)
        planned = _plan(memories)

        # An archive of nothing but unfinished recordings plans chapters that
        # would hold no blocks. Checked after planning rather than before, so
        # "no memories" and "no memories with anything in them" are one answer.
        if not any(_prose(m) for chapter in planned for m in chapter["memories"]):
            raise NothingToAssemble

        await cur.execute(
            "DELETE FROM chapter WHERE memoir_id = %(memoir_id)s",
            {"memoir_id": memoir_id},
        )

        tally = {"chapters": 0, "blocks": 0, "sources": 0, "figures": 0}
        ordinal = 0
        for chapter in planned:
            # A chapter that ended up with no prose is not created at all: its
            # photographs had nothing to anchor to and `chapter_title_not_blank`
            # would leave an empty page in the contents rail.
            if not any(_prose(m) for m in chapter["memories"]):
                continue
            written = await _write_chapter(cur, memoir_id, ordinal, chapter)
            ordinal += 1
            tally["chapters"] += 1
            for key in ("blocks", "sources", "figures"):
                tally[key] += written[key]

    logger.info(
        "Assembled memoir %s into %s chapters (%s blocks, %s figures)",
        memoir_id,
        tally["chapters"],
        tally["blocks"],
        tally["figures"],
    )
    return tally
