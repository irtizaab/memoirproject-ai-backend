# Domain layer for assembly — turning an archive into a book.
#
# Nothing here imports fastapi. It returns plain dicts and raises its own
# exceptions; api/chapters.py decides which status code each one is.
#
# ---------------------------------------------------------------------------
# Two assemblers, one writer
# ---------------------------------------------------------------------------
# Migration 0011 and `chapter_service.py` both describe assembly as the model
# call that reads the whole archive and produces blocks with attributed spans.
# It is built now, and it lives in `planner.py`. This file is what writes down
# whatever comes back.
#
# `_plan()` dispatches between the two. The planner composes prose from several
# people's memories and quotes itself so every character offset can be checked
# before it is stored. `_plan_by_date()` is what runs when the planner cannot —
# switched off, unreachable, or a plan that did not survive verification. It
# groups by decade, gives each memory its own block, and attributes that block
# to the person who left it, whole, with NULL offsets.
#
# The fallback is not a stub and does not go away. A memoir that cannot be
# assembled at all is unreachable — no reader, no export, no search over it —
# which is a worse outcome than a book divided by decade. It also cannot
# fabricate anything: it never rephrases, so every word a reader sees is a word
# a contributor typed or spoke. That is a weaker book than the planner produces
# and a stronger guarantee than the planner can make.
#
# Everything below `_plan()` is indifferent to which one ran. `_write_chapter`
# receives the same shape either way, and `block_source` says the same thing:
# who said this, and which part of it.
#
# ---------------------------------------------------------------------------
# Two steps, not one
# ---------------------------------------------------------------------------
# `generate_plan()` plans and stores; `assemble()` writes what was stored.
# They used to be one function, which meant the model's decisions about a
# family's book existed only for the duration of one request and were
# unreviewable by the person whose book it was. `plan_service.py` owns the row
# in between. What it changes here: the model call is in `generate_plan`, and
# `assemble` is Postgres from end to end and back to a single transaction.

import logging
from datetime import date

from src.domain.chapters import plan_service, planner
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

    The contributor's name and relationship are joined in for the planner,
    which needs to tell one account of an afternoon from another when it writes
    the paragraph that holds both. Nothing else here reads them — attribution
    is written from `participant_id`, never from a name.
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
               p.display_name AS contributor_name,
               COALESCE(p.relationship_label, p.relationship::text)
                   AS contributor_relationship,
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
          JOIN memoir_participant p ON p.id = m.participant_id
         WHERE m.memoir_id = %(memoir_id)s
         ORDER BY m.happened_on NULLS LAST, m.created_at
        """,
        {"memoir_id": memoir_id},
    )
    return await cur.fetchall()


async def _photographs(cur, memoir_id: str) -> list[dict]:
    """Every uploaded photograph in the memoir, in the archive's own order.

    Separate from `_memories` and not a join, because the relationship is
    one-to-many in the direction that matters: a memory can hold several
    photographs, and the planner is being asked about each one. Folding them
    into the memory rows would either lose all but the first — which is what
    `image_asset_id` does, and why it is only the fallback now — or repeat
    every memory's words once per picture.

    `uploaded_at IS NOT NULL` is the difference between a photograph and a
    reservation nobody completed. `storage_path` is here because the planner
    has to fetch the bytes; it never leaves the domain layer, and no response
    model in this product carries it.

    Ordered by the memory the photograph came with, so the list the model is
    shown runs in the same direction as the archive it is reading.
    """
    await cur.execute(
        """
        SELECT a.id,
               a.memory_id,
               a.storage_path,
               a.mime_type
          FROM media_asset a
          JOIN memory m ON m.id = a.memory_id
         WHERE a.memoir_id = %(memoir_id)s
           AND a.kind = 'image'
           AND a.uploaded_at IS NOT NULL
         ORDER BY m.happened_on NULLS LAST, m.created_at, a.created_at
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


def _blocks_verbatim(memories: list[dict]) -> list[dict]:
    """One block per memory, in that person's own words, attributed whole.

    The half of the deterministic assembler that cannot fabricate anything at
    all. It never rephrases, so there is no assembled sentence that could drift
    from what somebody actually said — which is why the offsets are NULL rather
    than computed. The paragraph *is* this person's words, so there is no
    clause of it that came from anywhere else.

    Emits the same shape the planner does, so `_write_chapter` cannot tell
    which of the two produced what it is writing.
    """
    blocks: list[dict] = []

    for memory in memories:
        text = _prose(memory)
        if text is None:
            continue

        blocks.append(
            {
                "kind": "paragraph",
                "text": text,
                "sources": [
                    {
                        "memory_id": memory["id"],
                        "participant_id": memory["participant_id"],
                        "start_offset": None,
                        "end_offset": None,
                        "diverges": False,
                    }
                ],
            }
        )

    return blocks


def _plan_by_date(memories: list[dict]) -> list[dict]:
    """Group memories into chapters by date alone. The fallback plan.

    Returns chapters in reading order. This is what runs whenever the model
    cannot: switched off, no key, unreachable, or a plan that did not survive
    verification. A memoir that cannot be assembled at all is a worse outcome
    than one assembled by decade, so this path never goes away.

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
                    "blocks": _blocks_verbatim(held),
                    "memories": held,
                    # The date assembler has no opinion about photographs. An
                    # empty list rather than a missing key, so `_write_chapter`
                    # has one shape to read and the positional rule below is
                    # reached by being empty rather than by being absent.
                    "figures": [],
                }
            )

    if undated:
        chapters.append(
            {
                "title": UNDATED_TITLE,
                "from_year": None,
                "through_year": None,
                "blocks": _blocks_verbatim(undated),
                "memories": undated,
                "figures": [],
            }
        )

    return chapters


async def _plan(
    memories: list[dict],
    memoir: dict,
    photographs: list[dict] | None = None,
    instructions: str | None = None,
) -> tuple[list[dict], str, planner.Outcome]:
    """Chapters in reading order, which assembler produced them, and the
    planner's full `Outcome` — its reason for standing aside, its reviewer's
    findings — for the plan row to keep.

    The dispatcher. It asks the planner to read the archive, and falls back to
    `_plan_by_date` whenever it cannot — see `domain/chapters/planner.py`,
    which returns None rather than raising for exactly this reason.

    The two plans differ in what a paragraph *is*. The planner composes prose
    from several memories and quotes itself so the offsets can be verified;
    the fallback prints one person's words unedited and attributes them whole.
    The reader renders both identically, because `block_source` says the same
    thing either way: who said this, and which part of it.

    The second element is the only thing that tells them apart afterwards.
    Without it a decade-banded book and a planned one are indistinguishable
    once written, which meant a deployment with no `GEMINI_API_KEY` produced
    the fallback for every memoir and said nothing about it — to the owner or
    to anybody reading the logs.
    """
    outcome = await planner.plan(
        memories, memoir, _prose, photographs or [], instructions
    )
    if outcome.chapters is not None:
        return outcome.chapters, "planner", outcome
    return _plan_by_date(memories), "by_date", outcome


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
    # memory id -> the first paragraph that quoted it. This is how a planned
    # figure's `anchor_memory_id` becomes a real `anchor_block_id`: the model
    # names a memory, and the row is resolved here from what was actually
    # written, never from anything the model said about position.
    paragraph_for: dict[str, str] = {}
    written = {"blocks": 0, "sources": 0, "figures": 0}

    for block in planned["blocks"]:
        await cur.execute(
            """
            INSERT INTO chapter_block (memoir_id, chapter_id, ordinal,
                                       kind, text)
            VALUES (%(memoir_id)s, %(chapter_id)s, %(ordinal)s,
                    %(kind)s::block_kind, %(text)s)
         RETURNING id
            """,
            {
                "memoir_id": memoir_id,
                "chapter_id": chapter_id,
                "ordinal": next_ordinal,
                "kind": block["kind"],
                "text": block["text"],
            },
        )
        block_id = (await cur.fetchone())["id"]
        next_ordinal += 1
        written["blocks"] += 1

        # A figure anchors to a paragraph, never to a pulled line. A pull is
        # one lifted sentence set apart from the flow, and a photograph in its
        # margin would be captioned by a fragment.
        if block["kind"] == "paragraph":
            last_paragraph_id = block_id
            for source in block["sources"]:
                paragraph_for.setdefault(str(source["memory_id"]), block_id)

        # The offsets arrive already checked. `planner.verify` computed each
        # one with `str.find` against this exact text and dropped anything it
        # could not locate, so nothing here has to trust a number a model
        # chose. The fallback plan sends NULLs, which mean the whole block came
        # from that one person — see `_blocks_verbatim`.
        for source in block["sources"]:
            await cur.execute(
                """
                INSERT INTO block_source (memoir_id, block_id, memory_id,
                                          participant_id, start_offset,
                                          end_offset, diverges)
                VALUES (%(memoir_id)s, %(block_id)s, %(memory_id)s,
                        %(participant_id)s, %(start_offset)s, %(end_offset)s,
                        %(diverges)s)
                """,
                {
                    "memoir_id": memoir_id,
                    "block_id": block_id,
                    "memory_id": source["memory_id"],
                    "participant_id": source["participant_id"],
                    "start_offset": source["start_offset"],
                    "end_offset": source["end_offset"],
                    "diverges": source["diverges"],
                },
            )
            written["sources"] += 1

    async def place(asset_id, placement: str, anchor: str | None) -> bool:
        """Write one figure. False if there was nothing to anchor it to.

        A photograph whose chapter contains no prose is skipped rather than
        stored: `block_figure_shape` makes `anchor_block_id` NOT NULL, and the
        alternative — anchoring it to a paragraph elsewhere in the life —
        would caption it with somebody else's afternoon.
        """
        nonlocal next_ordinal
        if anchor is None:
            logger.info(
                "Chapter %s has a photograph but no prose to anchor it to",
                chapter_id,
            )
            return False

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
                "asset_id": asset_id,
                "placement": placement,
                "anchor": anchor,
            },
        )
        next_ordinal += 1
        written["figures"] += 1
        return True

    planned_figures = planned.get("figures") or []

    if planned_figures:
        # The model looked at the photographs and said where each belongs.
        # Every asset id and anchor here has already been checked against the
        # archive by `planner.verify_figures`, and the anchor is resolved to a
        # row written moments ago rather than to anything it claimed about
        # order. `placement` includes 'carousel': several figures sharing one
        # anchor with that placement are one carousel, in the order below.
        for figure in planned_figures:
            await place(
                figure["asset_id"],
                figure["placement"],
                paragraph_for.get(str(figure["anchor_memory_id"]))
                or last_paragraph_id,
            )
    else:
        # No figure decisions, which means the date assembler ran, or the model
        # placed nothing. One photograph per memory that has one, the first
        # full measure and the rest in the margin. A rule rather than a
        # judgement, and the reason it is still here: a book divided by decade
        # with its photographs in the margin is a book.
        fallback = [
            memory
            for memory in planned["memories"]
            if memory["image_asset_id"] is not None
        ]
        for position, memory in enumerate(fallback):
            await place(
                memory["image_asset_id"],
                "inset" if position == 0 else "margin",
                last_paragraph_id,
            )

    return written


async def generate_plan(
    memoir_id: str, user_id: str, instructions: str | None = None
) -> dict | None:
    """Read the archive, decide what the book is, and store the plan.

    Returns the plan as the owner reads it, None if the memoir is not theirs.
    Raises NothingToAssemble on an empty archive and MemoirSealed on a
    published one.

    This is the expensive half of what `assemble()` used to be, and it is now
    the only half that calls a model. Nothing downstream of the archive is
    written: the plan is a draft, the owner is expected to read it, and
    running this again replaces it.

    ---------------------------------------------------------------------
    Why the model call sits between two transactions
    ---------------------------------------------------------------------
    Planning an archive can take minutes, and a Postgres connection held open
    across an HTTP call to another service is how a bounded pool dies — the
    same reason signed storage URLs are minted outside their transaction in
    `media_service`. So it sits between two, and the second one re-checks
    ownership and `status` before it writes anything.

    The cost is that a memoir published in between is planned and then
    discarded. That is the right way round: the alternative is a sealed memoir
    with a fresh plan attached to it, inviting an assembly that can never run.
    """
    async with db() as conn, conn.cursor() as cur:
        memoir = await owned_memoir(cur, memoir_id, user_id)
        if memoir is None:
            return None

        if memoir["status"] == "published":
            raise MemoirSealed

        memories = await _memories(cur, memoir_id)

        # Checked before planning, unlike everything else here, because there
        # is no point spending a model call on an archive with nothing in it —
        # and because an empty archive and one holding only unfinished
        # recordings should be the same answer to the owner.
        if not any(_prose(memory) for memory in memories):
            raise NothingToAssemble

        photographs = await _photographs(cur, memoir_id)

    planned, organised_by, outcome = await _plan(
        memories, memoir, photographs, instructions
    )

    async with db() as conn, conn.cursor() as cur:
        memoir = await owned_memoir(cur, memoir_id, user_id)
        if memoir is None:
            return None
        if memoir["status"] == "published":
            raise MemoirSealed

        # Belt and braces. The archive had prose a moment ago, so this only
        # fires if a plan came back with every block dropped — which `planner`
        # already guards against by falling back. Cheaper to check than to
        # explain a plan with zero chapters and a 200.
        if not any(chapter["blocks"] for chapter in planned):
            raise NothingToAssemble

        await plan_service.store(
            cur,
            memoir_id,
            planned,
            organised_by,
            reason=outcome.reason,
            review=outcome.review,
            guide=outcome.guide,
        )
        row = await plan_service.load(cur, memoir_id)

    logger.info(
        "Planned memoir %s into %s chapters, organised by %s",
        memoir_id,
        len(planned),
        organised_by,
    )
    return plan_service.summarise(row)


async def assemble(memoir_id: str, user_id: str) -> dict | None:
    """Write the stored plan into the memoir's chapters.

    Returns a tally of what was written, None if the memoir is not this user's.
    Raises NoPlanYet when nothing has planned the memoir, NothingToAssemble
    when every block in the stored plan cites memories that have since been
    deleted, and MemoirSealed on a published memoir.

    ---------------------------------------------------------------------
    Why this is one transaction again
    ---------------------------------------------------------------------
    It used to be two, with a model call in the gap. The model call moved to
    `generate_plan`, so everything left here is a read of the archive, a read
    of one row, and the writes — all of it Postgres, all of it fast. Holding a
    single connection for the whole of it is now the simplest correct thing,
    and it closes the window in which a memoir could be published between
    planning and writing.

    Everything that touches `chapter` happens in that one transaction, for the
    reason `claim_draft` gives at length: a memoir left holding half of one
    assembly and half of another is a book with paragraphs missing and no code
    path that would ever notice.

    ---------------------------------------------------------------------
    Why this replaces rather than appends
    ---------------------------------------------------------------------
    Assembly is re-runnable while the memoir is a draft, because the owner will
    add memories, regenerate the plan, and want the book to include them.
    Appending would duplicate every existing paragraph, so the previous
    chapters are deleted first and the whole book is rebuilt from the plan.

    The DELETE cascades to `chapter_block`, `block_source` and `comment_thread`
    — everything downstream of a chapter. Comments are the only loss that would
    matter, and a draft cannot have any: reaching the comment routes needs a
    live *view* link, and a view link is issued at publication.
    """
    async with db() as conn, conn.cursor() as cur:
        memoir = await owned_memoir(cur, memoir_id, user_id)
        if memoir is None:
            return None

        if memoir["status"] == "published":
            raise MemoirSealed

        row = await plan_service.load(cur, memoir_id)
        if row is None:
            raise plan_service.NoPlanYet

        stored = plan_service.summarise(row)

        # The archive is re-read rather than taken from the plan, so the book
        # is built from the memories as they stand now and not as they stood
        # when the model read them. `rehydrate` drops anything that has gone.
        memories = await _memories(cur, memoir_id)
        photographs = await _photographs(cur, memoir_id)
        planned = plan_service.rehydrate(
            {"chapters": stored["chapters"]}, memories, photographs
        )

        if not any(chapter["blocks"] for chapter in planned):
            raise NothingToAssemble

        await cur.execute(
            "DELETE FROM chapter WHERE memoir_id = %(memoir_id)s",
            {"memoir_id": memoir_id},
        )

        tally = {
            "chapters": 0,
            "blocks": 0,
            "sources": 0,
            "figures": 0,
            "organised_by": stored["organised_by"],
        }
        ordinal = 0
        for chapter in planned:
            # A chapter that ended up with no prose is not created at all: its
            # photographs had nothing to anchor to and `chapter_title_not_blank`
            # would leave an empty page in the contents rail.
            if not chapter["blocks"]:
                continue
            written = await _write_chapter(cur, memoir_id, ordinal, chapter)
            ordinal += 1
            tally["chapters"] += 1
            for key in ("blocks", "sources", "figures"):
                tally[key] += written[key]

        await plan_service.mark_assembled(cur, memoir_id)

    logger.info(
        "Assembled memoir %s into %s chapters (%s blocks, %s figures)",
        memoir_id,
        tally["chapters"],
        tally["blocks"],
        tally["figures"],
    )
    return tally
