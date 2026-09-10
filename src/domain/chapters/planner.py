# The model call that reads an archive and decides what the book is.
#
# Split out of `assembly_service.py` rather than added to it, because the two
# files answer different questions and fail in different ways. This one is
# "what should the chapters say"; that one is "write it down without breaking
# the schema". The writes are deterministic and tested; this is a model, and
# everything it returns is treated as a claim until checked.
#
# ---------------------------------------------------------------------------
# The rule this whole file is built around
# ---------------------------------------------------------------------------
# The model never returns a character offset. It returns a `quote` — the exact
# substring of its own paragraph that came from one person's memory — and
# `_verify` computes the offsets here, with `str.find`.
#
# The difference is the difference between a claim and a fact. An integer a
# model invents cannot be checked against anything: 41 is a plausible number
# whether or not the clause ending at character 41 came from that memory, and a
# wrong one silently credits somebody's sentence to somebody else's
# grandmother, permanently, because publication is immutable. A substring can
# be checked, because either it is in the text or it is not.
#
# `block_source` is where "never fabricate" stops being a promise and becomes
# something a reader can audit. It is worth this much care.

import logging

from pydantic import BaseModel, Field

from src.core.config import settings
from src.integrations import gemini

logger = logging.getLogger(__name__)

# How much of one memory to send. Long enough for a full recording of somebody
# telling a story, short enough that fifty of them fit in one prompt.
_MAX_MEMORY_CHARS = 6000


# ---------------------------------------------------------------------------
# The shape the model must answer in
# ---------------------------------------------------------------------------


class PlannedSource(BaseModel):
    """One memory a paragraph drew on, and which words came from it."""

    memory_id: str = Field(
        description="the id of the memory, copied exactly from the input"
    )
    quote: str | None = Field(
        default=None,
        description=(
            "the exact run of characters in this block's text that came from "
            "this memory, copied character for character from the text you "
            "wrote. Null if the whole block came from this memory."
        ),
    )
    diverges: bool = Field(
        default=False,
        description=(
            "true when this memory contradicts what the block says and was "
            "kept anyway"
        ),
    )


class PlannedBlock(BaseModel):
    """One paragraph, or one pulled line."""

    kind: str = Field(
        default="paragraph", description="either 'paragraph' or 'pull'"
    )
    text: str
    sources: list[PlannedSource] = Field(default_factory=list)


class PlannedChapter(BaseModel):
    """One chapter: a title, the years it covers, and its blocks in order."""

    title: str
    from_year: int | None = None
    through_year: int | None = None
    blocks: list[PlannedBlock] = Field(default_factory=list)


class Plan(BaseModel):
    """The whole book, in reading order."""

    chapters: list[PlannedChapter] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# The rules the model works under
# ---------------------------------------------------------------------------
#
# Every line is a product constraint from AGENTS.md. The `quote` rule is the
# load-bearing one: it is what makes the fabrication rule checkable rather than
# merely asserted.
_SYSTEM = """\
You are assembling a family's memoir. You are given every memory several \
people left about one person, each with an id, who left it, and what they \
said. You return the book: chapters, in reading order, made of paragraphs.

You may compose. Weave several people's memories of the same event into one \
paragraph, in your own connective prose, so it reads as a book rather than as \
a pile of quotations.

What you may never do:

- Never state anything you were not given. No dates, no places, no names, no \
outcomes, no feelings, no details of any kind that are not in the memories in \
front of you. If you find yourself needing a fact to finish a sentence, end \
the sentence earlier.
- Never resolve a contradiction. When two people remember the same event \
differently, keep both accounts in the text, attribute each, and set diverges \
to true on both. Do not decide who is right, do not average them, and do not \
say that accounts differ without saying how.
- Never write about the contributors' grief, and never address the reader.
- Never invent a caption for a photograph. Photographs are placed by other \
code and are not yours to describe.

Attribution is the point of this task, not paperwork attached to it:

- Every block must list a source for every clause in it. A sentence nobody \
said is a fabrication even if it is true.
- For each source, set quote to the EXACT run of characters in the block's own \
text that came from that memory — copied character for character out of the \
text you just wrote, including its punctuation and spacing. Do not quote the \
original memory; quote your paragraph. Set quote to null only when the whole \
block came from that one memory.
- Copy each memory_id exactly as given. Never invent one.

Chapters:

- Group by what actually divides this life — a move, a war, a marriage, a job, \
a house — not by decade, unless the archive gives you nothing better.
- Title each chapter in a few plain words. A title may be evocative; it is the \
one place here where writing something nobody said is wanted. Do not make it a \
sentence and do not put a year in it.
- from_year and through_year are the earliest and latest years the chapter's \
memories are dated to, or null when none of them are dated.
- Order chapters as a life runs. Undated material goes last.
- A 'pull' block is one short line lifted for emphasis. Use it rarely or not \
at all."""


# ---------------------------------------------------------------------------
# Checking what came back
# ---------------------------------------------------------------------------


def verify(block_text: str, sources: list[PlannedSource], allowed: dict) -> list[dict]:
    """Turn the model's claimed sources into ones the database can trust.

    `allowed` maps memory id to the participant who left it — built from the
    archive we actually read, so it is the only authority on which memories
    exist and who they belong to. The participant is never taken from the
    model: it did not need it and could not know it better than the row does.

    Four outcomes, in order of how much is kept:

      quote found exactly once   real `start_offset` / `end_offset`
      quote null                 whole-block attribution, NULL offsets — the
                                 model said this block is one person's
      quote absent or ambiguous  whole-block attribution, NULL offsets, logged.
                                 The credit is still correct; only its
                                 precision is lost, and a wrong offset is worse
                                 than no offset
      unknown memory id          dropped. A hallucinated id would be rejected
                                 by the foreign key anyway; better to notice it
                                 here, where it can be counted

    A source appearing twice for one memory is collapsed to the first. The
    reader prints one credit per person per block and two rows would print two.
    """
    verified: list[dict] = []
    seen: set[str] = set()

    for source in sources:
        memory_id = (source.memory_id or "").strip()
        if memory_id not in allowed:
            logger.warning("Assembly cited a memory that is not in the archive")
            continue
        if memory_id in seen:
            continue
        seen.add(memory_id)

        start: int | None = None
        end: int | None = None

        quote = (source.quote or "").strip()
        if quote:
            first = block_text.find(quote)
            if first == -1:
                logger.info("Assembly quoted text that is not in its own block")
            elif block_text.find(quote, first + 1) != -1:
                # Ambiguous. "the house" may appear four times in a paragraph
                # and there is no way to know which one was meant, so the
                # credit falls back to the whole block rather than guessing.
                logger.info("Assembly quote appears more than once in its block")
            else:
                start, end = first, first + len(quote)

        verified.append(
            {
                "memory_id": memory_id,
                "participant_id": allowed[memory_id],
                "start_offset": start,
                "end_offset": end,
                "diverges": bool(source.diverges),
            }
        )

    return verified


def _clean(plan: Plan, memories: list[dict]) -> list[dict]:
    """The plan, reduced to what is safe to write. Chapters in reading order.

    Returns the same shape `assembly_service._plan_by_date` returns, so the
    writer has one thing to write and cannot tell which produced it.

    Three things are dropped, each because storing it would be worse than
    losing it:

      a block with no text            `block_prose_shape` forbids it
      a block with no valid source    an unattributed paragraph in this product
                                      is a fabricated one
      a chapter with no blocks left   `chapter_title_not_blank` would leave an
                                      empty page in the contents rail
    """
    allowed = {str(m["id"]): m["participant_id"] for m in memories}
    by_id = {str(m["id"]): m for m in memories}

    chapters: list[dict] = []

    for planned in plan.chapters:
        blocks: list[dict] = []
        cited: set[str] = set()

        for block in planned.blocks:
            text = (block.text or "").strip()
            if not text:
                continue

            sources = verify(text, block.sources, allowed)
            if not sources:
                logger.warning("Dropped an assembled block with no valid source")
                continue

            blocks.append(
                {
                    # Anything the model invents that is not a known block kind
                    # becomes a paragraph. The enum would reject it, and a
                    # paragraph is what it almost certainly is.
                    "kind": "pull" if block.kind == "pull" else "paragraph",
                    "text": text,
                    "sources": sources,
                }
            )
            cited.update(source["memory_id"] for source in sources)

        if not blocks:
            continue

        title = (planned.title or "").strip()
        if not title:
            # Not fatal and not worth losing a chapter over. The owner renames
            # chapters before publishing anyway.
            title = "Untitled"

        chapters.append(
            {
                "title": title,
                "from_year": planned.from_year,
                "through_year": planned.through_year,
                "blocks": blocks,
                # The memories this chapter drew on, in archive order. The
                # writer's photograph pass reads this: a photograph belongs in
                # the chapter that used the memory it came with.
                "memories": [by_id[mid] for mid in by_id if mid in cited],
            }
        )

    return chapters


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


def _describe(memories: list[dict], memoir: dict, prose) -> str:
    """The archive, as labelled records the model can cite by id.

    `prose` is `assembly_service._prose`, passed in rather than imported, so
    that the words sent to the model are byte-for-byte the words the
    deterministic assembler would have written. If those two ever diverged, a
    quote could match the model's input and not the block it ends up in.
    """
    header = [f"The memoir is about: {memoir['subject_name']}"]

    lines: list[str] = []
    for memory in memories:
        text = prose(memory)
        if text is None:
            # Nothing to compose from — an unfinished recording, usually. It
            # stays in the archive; it just cannot be quoted.
            continue

        when = memory["happened_on"] or "date unknown"
        who = memory["contributor_name"]
        if memory["contributor_relationship"]:
            who = f"{who} ({memory['contributor_relationship']})"

        lines.append(
            f"---\n"
            f"memory_id: {memory['id']}\n"
            f"left by: {who}\n"
            f"about: {when}\n"
            f"words:\n{text[:_MAX_MEMORY_CHARS]}"
        )

    return "\n\n".join(header + lines)


async def plan(memories: list[dict], memoir: dict, prose) -> list[dict] | None:
    """Read the archive and return chapters, or None if the model could not.

    None means "use the deterministic assembler" and covers every failure:
    switched off, no key, unreachable, refused, malformed, or a plan that
    survived verification with nothing left in it. The caller falls back rather
    than raising, because a memoir that cannot be assembled at all is a worse
    outcome than one assembled by decade.

    Runs on `gemini_assembly_model` — the slower tier. This call happens once,
    by hand, over an entire archive, and the judgement it needs is the whole
    reason it exists.
    """
    described = _describe(memories, memoir, prose)

    try:
        result = await gemini.generate(
            described,
            Plan,
            system=_SYSTEM,
            model=settings.gemini_assembly_model,
            # Lower than the follow-up questions use. This is composition
            # bounded by evidence — every clause has to be quotable back to a
            # memory — and a higher temperature buys invention, which is the
            # one thing forbidden here.
            temperature=0.3,
            # Minutes, not seconds. It is reading a whole family archive and
            # writing a book out of it, and nobody is watching a spinner: the
            # owner pressed a button and expects to wait.
            timeout=300.0,
        )
    except gemini.GeminiDisabled:
        logger.info("Assembly is planning by date: the model is switched off")
        return None
    except gemini.GeminiError as exc:
        logger.warning("Assembly is planning by date: %s", exc)
        return None

    chapters = _clean(result, memories)
    if not chapters:
        logger.warning("Assembly is planning by date: nothing survived verification")
        return None

    return chapters
