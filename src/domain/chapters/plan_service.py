# The plan row: what the model decided, held where the owner can read it.
#
# Nothing here imports fastapi, and nothing here calls a model. This file is
# the persistence and the shape-checking for `memoir_plan` — one row per
# memoir, holding the output of `planner.py` between the model call and the
# book.
#
# ---------------------------------------------------------------------------
# Why this file does not import assembly_service
# ---------------------------------------------------------------------------
# Because assembly_service imports this one. Reading the archive, dispatching
# to the planner and writing chapters all live there and stay there; what
# lives here is the row and the rules about the row. Kept one-directional on
# purpose — the alternative was a cycle between the two halves of one
# operation, resolved by a local import inside a function, which is how the
# layering test starts passing while the layering stops being true.
#
# ---------------------------------------------------------------------------
# What is in the document and what is deliberately not
# ---------------------------------------------------------------------------
# `_document` stores chapters, blocks, verified sources, and per chapter a
# list of `memory_ids`. It does not store memory rows. The archive is the
# authority on what a memory says and who left it, so `rehydrate` reads it
# again at assembly time and joins by id.
#
# That is not only about duplication. A memory can be deleted or rewritten
# between generating a plan and assembling it, and a plan that had copied the
# rows would happily insert a `block_source` pointing at a memory that no
# longer exists — a foreign key violation at best, and at worst a paragraph
# that quotes something the owner has since taken out of their own archive.
# `rehydrate` drops what has gone, and drops any block left with no source,
# which is the same rule `planner._clean` applies for the same reason.

import json
import logging
from uuid import uuid4

from psycopg.types.json import Jsonb

from src.domain.memoirs.access import owned_memoir
from src.integrations.db import db

logger = logging.getLogger(__name__)

# The two things `organised_by` may say, mirroring the CHECK constraint in
# migration 0015. `planner` means the model read the archive; `by_date` means
# it could not and the decade fallback ran.
ORIGINS = ("planner", "by_date")


class NoPlanYet(Exception):
    """The memoir is real and it is yours, and nothing has planned it.

    Distinct from "no such memoir", which is what `None` means everywhere in
    this package. The owner needs to be told to generate a plan, not that
    their memoir is missing.
    """


class PlanSealed(Exception):
    """The memoir is published, so its plan is a record and not a draft.

    The twin of `assembly_service.MemoirSealed`, declared here rather than
    imported from there because that module imports this one and a cycle
    resolved by a function-local import is a layering rule that has stopped
    being true. The route maps both to the same 409.
    """


class NothingLeft(Exception):
    """The edit removed everything. A plan with no chapters is not a plan.

    Refused rather than stored, so the owner cannot arrive at a state where
    the only thing they can do is regenerate — and so `assemble` never has to
    answer for an empty document somebody saved on purpose.
    """


# There used to be a `PlanAlreadyAssembled` here, refusing any edit once
# `assembled_at` was set. It was wrong about which fact protects the offsets.
#
# What makes a character offset permanent is **publication**, not assembly: an
# unpublished book has no readers and no comment layer, and `assemble()`
# rewrites its chapters from scratch every time. So a plan stays a draft for as
# long as the memoir is unsealed, and assembling again is how the owner applies
# an outline they have changed since.
#
# The cost is real and belongs in the interface rather than in a refusal:
# assembling again replaces the chapters, so anything the owner corrected by
# hand on the page (`page_service`) is written over. `PlanOutline` says so
# before the button is pressed.


# ---------------------------------------------------------------------------
# Turning chapters into a document and back
# ---------------------------------------------------------------------------


def _document(chapters: list[dict]) -> dict:
    """The plan as JSON, with the archive left out of it.

    Takes what `planner._clean` and `assembly_service._plan_by_date` both
    return — the shape `_write_chapter` consumes — and reduces it to something
    storable: ids as strings, memory rows replaced by their ids.

    The ids arrive from psycopg as `UUID` objects and the offsets as `int` or
    `None`. Everything is stringified here rather than at the edges, because
    the one thing this document must survive is a round trip through jsonb,
    and a `UUID` does not.
    """
    stored: list[dict] = []

    for chapter in chapters:
        stored.append(
            {
                # An id the owner's screen can name a chapter by.
                #
                # Minted here rather than by the model, and not derived from
                # the title or the position: a rename must not change which
                # chapter is being renamed, and neither must a reorder. It
                # lives only as long as this plan — regenerating makes new
                # ones, because they are new chapters.
                "id": str(uuid4()),
                "title": chapter["title"],
                "from_year": chapter["from_year"],
                "through_year": chapter["through_year"],
                "blocks": [
                    {
                        "kind": block["kind"],
                        "text": block["text"],
                        "sources": [
                            {
                                "memory_id": str(source["memory_id"]),
                                "start_offset": source["start_offset"],
                                "end_offset": source["end_offset"],
                                "diverges": bool(source["diverges"]),
                            }
                            for source in block["sources"]
                        ],
                    }
                    for block in chapter["blocks"]
                ],
                # Where the model decided each photograph belongs. Stored as
                # the model's own vocabulary — an asset and the memory whose
                # paragraph it sits beside — and not as block ids, because the
                # blocks do not exist until `assemble()` writes them and would
                # be different rows on the next run.
                "figures": [
                    {
                        "asset_id": str(figure["asset_id"]),
                        "anchor_memory_id": str(figure["anchor_memory_id"]),
                        "placement": figure["placement"],
                    }
                    for figure in chapter.get("figures", [])
                ],
                # `participant_id` is deliberately absent from the sources
                # above and rebuilt on rehydrate from the memory row. It is
                # never taken from the model and there is no reason to take it
                # from a stored document either: the archive knows who left a
                # memory, and if that ever changed, the plan should follow.
                "memory_ids": [
                    str(memory["id"]) for memory in chapter.get("memories", [])
                ],
            }
        )

    return {"chapters": stored}


def _live_figures(
    figures: list[dict], live_assets: set[str], by_id: dict
) -> list[dict]:
    """The planned figures whose photograph and anchoring memory both survive.

    Both halves have to still exist. A missing asset fails the foreign key on
    `chapter_block`; a missing anchor memory means no paragraph will cite it,
    so `_write_chapter` would fall back to the last paragraph in the chapter
    and put the photograph beside prose it has nothing to do with.

    An empty `live_assets` means the caller did not pass the archive's
    photographs at all, which is how `rehydrate` stays callable from a test
    that only cares about prose. It costs the figures, which is the safe
    direction.
    """
    kept: list[dict] = []

    for figure in figures:
        if str(figure["asset_id"]) not in live_assets:
            logger.info("Plan places a photograph that is no longer in the archive")
            continue
        if str(figure["anchor_memory_id"]) not in by_id:
            logger.info("Plan anchors a photograph to a memory that is gone")
            continue
        kept.append(figure)

    return kept


def rehydrate(
    body: dict, memories: list[dict], photographs: list[dict] | None = None
) -> list[dict]:
    """The stored document joined back to the archive, ready for the writer.

    Returns exactly what `assembly_service._write_chapter` expects, so the
    writer still cannot tell whether a model or a decade produced what it is
    writing — and now also cannot tell whether it came straight from the
    planner or out of a row.

    Three things drop out here, all of them because the archive moved on since
    the plan was made:

      a source whose memory is gone   the memory was deleted. The foreign key
                                      would reject it; better to notice it
                                      here, where it can be counted
      a block with no sources left    an unattributed paragraph in this
                                      product is a fabricated one
      a chapter with no blocks left   `chapter_title_not_blank` would leave an
                                      empty page in the contents rail
      a figure whose photograph or
      whose anchoring memory is gone   the owner deleted it. `chapter_block`
                                      foreign-keys the asset, so this is the
                                      difference between one missing picture
                                      and a 500 on a button

    Note what does *not* drop out: a source whose memory still exists but
    whose text has been edited. Its offsets were verified against the block
    text stored in the plan, not against the memory, and the block text is
    what a reader sees — so the span is still true about the paragraph it
    points into. `planner.verify` is the authority on that, and it already
    ran.
    """
    by_id = {str(memory["id"]): memory for memory in memories}
    live_assets = {str(a["id"]) for a in photographs or []}
    chapters: list[dict] = []

    for stored in body.get("chapters", []):
        wanted = set(stored.get("memory_ids", []))
        blocks: list[dict] = []

        for block in stored.get("blocks", []):
            sources: list[dict] = []
            for source in block.get("sources", []):
                memory = by_id.get(source["memory_id"])
                if memory is None:
                    logger.info(
                        "Plan cites a memory that is no longer in the archive"
                    )
                    continue
                sources.append(
                    {
                        "memory_id": memory["id"],
                        "participant_id": memory["participant_id"],
                        "start_offset": source["start_offset"],
                        "end_offset": source["end_offset"],
                        "diverges": bool(source["diverges"]),
                    }
                )

            if not sources:
                logger.info("Dropped a planned block whose memories are gone")
                continue

            blocks.append(
                {
                    "kind": block["kind"],
                    "text": block["text"],
                    "sources": sources,
                }
            )

        if not blocks:
            continue

        chapters.append(
            {
                "id": stored.get("id"),
                "title": stored["title"],
                "from_year": stored["from_year"],
                "through_year": stored["through_year"],
                "blocks": blocks,
                "figures": _live_figures(
                    stored.get("figures", []), live_assets, by_id
                ),
                # Archive order, not plan order, matching what
                # `planner._clean` promises the figure pass.
                "memories": [
                    memory for memory in memories if str(memory["id"]) in wanted
                ],
            }
        )

    return chapters


# ---------------------------------------------------------------------------
# The row
# ---------------------------------------------------------------------------


async def store(
    cur,
    memoir_id: str,
    chapters: list[dict],
    organised_by: str,
    reason: str | None = None,
    review: dict | None = None,
    guide: str | None = None,
) -> None:
    """Write the plan, replacing whatever was there.

    Generating again is how the owner asks for a different book, so this is an
    upsert and not an insert: one plan per memoir, and the previous draft is
    what they asked to be rid of. `edited_at` and `assembled_at` reset to NULL
    with it — they described the document being replaced.
    """
    if organised_by not in ORIGINS:
        raise ValueError(f"unknown plan origin: {organised_by}")

    await cur.execute(
        """
        INSERT INTO memoir_plan (memoir_id, body, organised_by)
        VALUES (%(memoir_id)s, %(body)s, %(organised_by)s)
   ON CONFLICT (memoir_id) DO UPDATE
           SET body         = EXCLUDED.body,
               organised_by = EXCLUDED.organised_by,
               generated_at = now(),
               edited_at    = NULL,
               assembled_at = NULL
        """,
        {
            "memoir_id": memoir_id,
            # Why it fell back to decades, and what the reviewer found — kept
            # inside the document rather than in columns: they are only ever
            # read alongside the plan, and they are replaced with it.
            "body": Jsonb(
                {
                    **_document(chapters),
                    "reason": reason,
                    "review": review,
                    "guide": guide,
                }
            ),
            "organised_by": organised_by,
        },
    )


async def load(cur, memoir_id: str) -> dict | None:
    """The plan row, or None if this memoir has never been planned.

    No ownership check — every caller has already resolved the memoir through
    `owned_memoir`, and a function that takes a cursor and a memoir id is not
    where authorization belongs.
    """
    await cur.execute(
        """
        SELECT body, organised_by, generated_at, edited_at, assembled_at
          FROM memoir_plan
         WHERE memoir_id = %(memoir_id)s
        """,
        {"memoir_id": memoir_id},
    )
    return await cur.fetchone()


def _extras(row: dict) -> dict:
    """Everything in a plan's body but the chapters."""
    body = row["body"]
    if isinstance(body, str):
        body = json.loads(body)
    return {k: v for k, v in body.items() if k != "chapters"}


def summarise(row: dict) -> dict:
    """A `memoir_plan` row as the owner reads it.

    Shared by `read` and by `assembly_service.generate_plan`, so generating a
    plan and fetching it back return the same document rather than two shapes
    the frontend has to tell apart.
    """
    body = row["body"]
    # psycopg returns jsonb as the decoded object already. The isinstance is
    # for the one case where a driver or a test hands back the raw string —
    # cheaper than a bug that only appears in one of them.
    if isinstance(body, str):
        body = json.loads(body)

    chapters = []
    for chapter in body.get("chapters", []):
        shown = dict(chapter)
        # The index each passage will be named by if the owner edits it. Sent
        # rather than left to the client to count, because the two have to
        # agree and only one of them is authoritative.
        shown["blocks"] = [
            {**block, "index": position}
            for position, block in enumerate(chapter.get("blocks", []))
        ]
        chapters.append(shown)

    return {
        "organised_by": row["organised_by"],
        "reason": body.get("reason"),
        "review": body.get("review"),
        "guide": body.get("guide"),
        "generated_at": row["generated_at"],
        "edited_at": row["edited_at"],
        "assembled_at": row["assembled_at"],
        "chapters": chapters,
    }


async def mark_assembled(cur, memoir_id: str) -> None:
    """Record that the book downstream was built from this plan."""
    await cur.execute(
        """
        UPDATE memoir_plan
           SET assembled_at = now()
         WHERE memoir_id = %(memoir_id)s
        """,
        {"memoir_id": memoir_id},
    )


# ---------------------------------------------------------------------------
# Editing it
# ---------------------------------------------------------------------------


class UnknownChapter(Exception):
    """The submitted plan names a chapter this one does not hold.

    Dropping a chapter is allowed — leaving it out of the list is how the owner
    removes it. Adding one is not: a chapter nobody planned has no memories
    behind it, and a paragraph with no source is a fabricated one.
    """


def resurvey(old_text: str, new_text: str, sources: list[dict]) -> list[dict]:
    """Re-place each source's span after the owner has edited the text.

    The rule the whole editing feature rests on, and the reason a plan can be
    edited at all.

    `block_source.start_offset`/`end_offset` are character positions into block
    text. They were computed by `planner.verify` with `str.find` against the
    text as the model wrote it. Edit that text and every offset past the edit
    is wrong — pointing at the middle of a word, or at a clause somebody else
    said. Publication is immutable, so a wrong offset is wrong forever, and
    `comment_thread` will eventually anchor into the same characters.

    So each span is re-found rather than adjusted: take the exact substring the
    offsets used to cover, look for it in the new text, and keep the span only
    if it is there exactly once. Anything else — gone, or now ambiguous — falls
    back to whole-block attribution with NULL offsets. The credit stays
    correct; only its precision is lost, which is the same trade
    `planner.verify` makes for the same reason.

    Unchanged text is returned untouched, which is the common case: renaming a
    chapter or moving one does not touch a single character of prose.
    """
    if old_text == new_text:
        return sources

    resurveyed: list[dict] = []

    for source in sources:
        start = source.get("start_offset")
        end = source.get("end_offset")

        if start is None or end is None:
            # Already whole-block. Nothing to lose.
            resurveyed.append(dict(source))
            continue

        span = old_text[start:end]
        first = new_text.find(span) if span else -1

        if first == -1 or new_text.find(span, first + 1) != -1:
            logger.info("An edited passage lost the span for one of its sources")
            new_start, new_end = None, None
        else:
            new_start, new_end = first, first + len(span)

        moved = dict(source)
        moved["start_offset"] = new_start
        moved["end_offset"] = new_end
        resurveyed.append(moved)

    return resurveyed


def _merge(stored: dict, submitted: dict) -> dict | None:
    """One chapter, as edited. None if nothing is left of it.

    What the owner may change, and what they may not:

      title              theirs. Blank falls back to what it was, then to
                         "Untitled" — `chapter_title_not_blank` refuses empty,
                         and losing a chapter over a stray backspace is worse
                         than an ugly title
      block order        theirs, by array position
      block text         theirs, at the cost of the spans — see `resurvey`
      blocks dropped     theirs
      figures            theirs: placement, anchor, or dropped entirely
      sources            **not** theirs. Which memory a passage came from is
                         read from the stored plan by index, never from the
                         request. A client that could reassign attribution
                         could credit anybody's sentence to anybody's
                         grandmother
      memory_ids         not theirs either. It is derived from what the blocks
                         cite, recomputed below

    A block is matched to its stored twin by position in the stored list,
    carried on the request as `index`. Not by text, because the text is the
    thing being edited; not by an id, because a passage does not have one — it
    is not a row yet.
    """
    by_index = {position: block for position, block in enumerate(stored["blocks"])}
    blocks: list[dict] = []
    cited: set[str] = set()

    for block in submitted.get("blocks", []):
        index = block.get("index")
        original = by_index.get(index)
        if original is None:
            logger.info("An edited plan named a passage that is not in it")
            continue

        text = (block.get("text") or "").strip()
        if not text:
            # An emptied passage is a deleted passage, which is what dropping
            # it would have said. Nothing to store either way.
            continue

        sources = resurvey(original["text"], text, original["sources"])
        if not sources:
            continue

        blocks.append(
            {
                "kind": original["kind"],
                "text": text,
                "sources": sources,
            }
        )
        cited.update(source["memory_id"] for source in sources)

    if not blocks:
        return None

    # Stringified on the way in. The stored document holds ids as strings —
    # jsonb has no uuid — while a validated request holds `UUID` objects, and
    # `UUID("...") in {"..."}` is quietly False rather than an error. That is
    # the kind of mismatch that silently drops every photograph and looks like
    # the model not placing any.
    known_assets = {str(f["asset_id"]) for f in stored.get("figures", [])}
    figures = []
    for figure in submitted.get("figures", []):
        asset_id = str(figure["asset_id"])
        anchor_id = str(figure["anchor_memory_id"])

        # The same asymmetry as chapters: a photograph can be dropped or moved,
        # but only one the plan already placed. `anchor_memory_id` has to be a
        # memory this chapter still quotes, or the figure would end up beside
        # prose it has nothing to do with.
        if asset_id not in known_assets:
            logger.info("An edited plan placed a photograph the plan does not hold")
            continue
        if anchor_id not in cited:
            logger.info("An edited plan anchored a photograph outside its chapter")
            continue
        if figure["placement"] not in ("margin", "inset", "carousel"):
            continue

        figures.append(
            {
                "asset_id": asset_id,
                "anchor_memory_id": anchor_id,
                "placement": figure["placement"],
            }
        )

    title = (submitted.get("title") or "").strip() or stored["title"] or "Untitled"

    return {
        "id": stored["id"],
        "title": title,
        "from_year": stored["from_year"],
        "through_year": stored["through_year"],
        "blocks": blocks,
        "figures": figures,
        # Recomputed, not submitted: it is a fact about which memories the
        # surviving passages cite, and a dropped passage should drop its
        # memory's photographs with it.
        "memory_ids": [mid for mid in stored["memory_ids"] if mid in cited],
    }


async def edit(memoir_id: str, user_id: str, chapters: list[dict]) -> dict | None:
    """Apply the owner's corrections. None if the memoir is not theirs.

    Raises `NoPlanYet`, `UnknownChapter`, `NothingLeft`, or `PlanSealed` once
    the memoir is published. An already-assembled plan is still editable —
    see the note where `PlanAlreadyAssembled` used to be.

    Chapter order is array order, and every id has to be one the stored plan
    holds. Nothing here writes to the book: this edits the draft, and
    `assemble()` is still what turns it into chapters.
    """
    async with db() as conn, conn.cursor() as cur:
        memoir = await owned_memoir(cur, memoir_id, user_id)
        if memoir is None:
            return None

        if memoir["status"] == "published":
            raise PlanSealed

        row = await load(cur, memoir_id)
        if row is None:
            raise NoPlanYet

        body = summarise(row)
        stored_by_id = {str(c["id"]): c for c in body["chapters"]}

        submitted_ids = [str(c["id"]) for c in chapters]
        if len(set(submitted_ids)) != len(submitted_ids):
            raise UnknownChapter("a chapter was listed twice")
        unknown = [cid for cid in submitted_ids if cid not in stored_by_id]
        if unknown:
            raise UnknownChapter("no such chapter in this plan")

        edited: list[dict] = []
        for chapter in chapters:
            merged = _merge(stored_by_id[str(chapter["id"])], chapter)
            if merged is not None:
                edited.append(merged)

        if not edited:
            raise NothingLeft

        await cur.execute(
            """
            UPDATE memoir_plan
               SET body      = %(body)s,
                   edited_at = now()
             WHERE memoir_id = %(memoir_id)s
            """,
            {
                "memoir_id": memoir_id,
                # The rest of the body — reason, review, guide — describes
                # this plan and stays with it through an edit.
                "body": Jsonb({**_extras(row), "chapters": edited}),
            },
        )

        return summarise(await load(cur, memoir_id))


# ---------------------------------------------------------------------------
# Reading it as the owner
# ---------------------------------------------------------------------------


async def read(memoir_id: str, user_id: str) -> dict | None:
    """The plan as the owner should see it. None if the memoir is not theirs.

    Raises `NoPlanYet` when the memoir is theirs and nothing has planned it.
    """
    async with db() as conn, conn.cursor() as cur:
        memoir = await owned_memoir(cur, memoir_id, user_id)
        if memoir is None:
            return None

        row = await load(cur, memoir_id)
        if row is None:
            raise NoPlanYet

        return summarise(row)
