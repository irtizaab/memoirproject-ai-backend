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

import io
from dataclasses import dataclass
import logging

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from src.core.config import settings
from src.integrations import gemini, supabase_storage

logger = logging.getLogger(__name__)

# How much of one memory to send. Long enough for a full recording of somebody
# telling a story, short enough that fifty of them fit in one prompt.
_MAX_MEMORY_CHARS = 6000

# ---------------------------------------------------------------------------
# How much photograph to send
# ---------------------------------------------------------------------------
# The model is shown the pictures, not told about them, because the judgement
# being asked for — is this photograph the moment that paragraph describes —
# cannot be made from a filename. That means the bytes go in the request, and
# the request has a size.
#
# Every image is re-encoded to at most `_MAX_IMAGE_EDGE` on its long side as
# JPEG. Nothing about the decision needs more: whether a photograph is a
# wedding, a dock or a hospital room is legible at 768px, and a 12-megapixel
# original costs forty times the bytes to say the same thing.
_MAX_IMAGE_EDGE = 768
_JPEG_QUALITY = 75

# What we will pull out of storage for one photograph before deciding it is
# not worth it. Generous — this is a raw camera file, not a thumbnail — and it
# exists so one enormous object cannot stall the whole plan.
_MAX_ASSET_BYTES = 25 * 1024 * 1024

# The ceiling on what goes into one request, after shrinking. Past it the
# remaining photographs are left out of the model's view, in archive order, and
# the count is logged. They are still in the archive and still placed in the
# book — by the positional rule in `assembly_service` — they simply were not
# looked at.
#
# ponytail: hard cap with a warning, roughly 150 photographs at the size
# above. The upgrade path when a family archive outgrows it is Gemini's Files
# API — upload once, reference by URI, no inline ceiling — not a bigger number
# here.
_MAX_INLINE_BYTES = 15 * 1024 * 1024


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


class PlannedFigure(BaseModel):
    """One photograph, and where in the chapter it belongs.

    The model chooses `anchor_memory_id` rather than a block index, for the
    same reason it quotes rather than counting characters: an index into a
    list it is also generating is a number with nothing to check it against,
    while a memory id either is in the archive or is not. `_clean` resolves it
    to a real block, and `assembly_service` resolves that to a row.
    """

    asset_id: str = Field(
        description="the id of the photograph, copied exactly from the input"
    )
    anchor_memory_id: str = Field(
        description=(
            "the id of the memory whose paragraph this photograph belongs "
            "beside, copied exactly from the input"
        )
    )
    placement: str = Field(
        default="margin",
        description=(
            "'inset' for a photograph the page should stop for, 'margin' for "
            "one that sits beside the prose, 'carousel' for one of several "
            "that belong to the same moment and should be shown in turn"
        ),
    )


class PlannedChapter(BaseModel):
    """One chapter: a title, the years it covers, and its blocks in order."""

    title: str
    from_year: int | None = None
    through_year: int | None = None
    blocks: list[PlannedBlock] = Field(default_factory=list)
    figures: list[PlannedFigure] = Field(default_factory=list)


class Plan(BaseModel):
    """The whole book, in reading order."""

    chapters: list[PlannedChapter] = Field(default_factory=list)


class Finding(BaseModel):
    """One thing the reviewer noticed about the draft.

    `kind` is a closed set the frontend can group by: `attribution` (a clause
    with no memory behind it), `structure` (chapters divided or ordered against
    the archive), `contradiction` (two accounts merged into one), `instruction`
    (the owner's guidance not followed), `thin` (a memory too short to compose
    from). Anything else the model invents becomes `structure`.
    """

    kind: str = Field(default="structure")
    chapter: str | None = Field(
        default=None, description="Title of the chapter concerned, or null."
    )
    note: str
    fixed: bool = Field(
        default=False, description="True only if the revised plan addresses it."
    )


class Review(BaseModel):
    """What the reviewer found, and the draft as it would correct it."""

    findings: list[Finding] = Field(default_factory=list)
    revised: Plan | None = Field(
        default=None, description="The corrected plan, or null to keep the draft."
    )


FINDING_KINDS = ("attribution", "structure", "contradiction", "instruction", "thin")
MAX_FINDINGS = 12


@dataclass
class Outcome:
    """What planning produced, all of it.

    `chapters` is None when the deterministic assembler should run, and
    `reason` then says why in the owner's terms. `review` is the reviewer's
    findings and whether its revision was taken — present whenever the
    reviewer ran, whichever way the plan went.
    """

    chapters: list[dict] | None
    reason: str | None = None
    review: dict | None = None
    guide: str | None = None


class GuideNote(BaseModel):
    """What the guide says to the owner, and what it tells the builder."""

    note: str = Field(default="")
    instructions_for_builder: str | None = Field(
        default=None,
        description=(
            "The owner's request restated precisely for the builder, or null "
            "when there was no request."
        ),
    )


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
- Never invent a caption for a photograph, and never describe one in the \
prose. You can see the photographs so that you know where they belong, not so \
that you can write about what is in them. A caption is the words the person \
who sent it wrote, or nothing.

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
at all.

Photographs:

- You are shown each photograph, labelled with its asset_id and the memory it \
was sent with. Put each one in the chapter that holds that moment, and set \
anchor_memory_id to the memory whose paragraph it belongs beside — the \
paragraph that describes what the photograph shows, when there is one.
- placement is a judgement about the page. Use 'inset' for a photograph worth \
stopping the page for; 'margin' for one that belongs beside the prose; \
'carousel' when several photographs are of the same moment and should be shown \
in turn rather than all at once. Give several photographs the same \
anchor_memory_id and 'carousel' to group them.
- Copy each asset_id exactly as given, and never invent one. A photograph you \
were not shown does not exist.
- Leaving a photograph out is allowed. A picture that does not belong anywhere \
in particular is better left for the archive than pushed against a paragraph \
it has nothing to do with."""


# ---------------------------------------------------------------------------
# The reviewer
# ---------------------------------------------------------------------------
#
# A second model reads the draft against the same archive and says what is
# wrong with it. It is not trusted any more than the first: its revision goes
# through `_clean` like the draft did, and `_accept_revision` refuses one that
# attributes worse than what it was handed. What it adds is a set of findings
# the owner can read — the draft used to arrive with no account of itself.

_REVIEW_SYSTEM = """\
You are reviewing a draft of a family's memoir against the memories it was \
written from. You are given the memories, each with an id, who left it, and \
what they said; then the draft plan as JSON. You return findings, and \
optionally a corrected plan.

What to look for, and the kind to file it under:

- attribution: any clause in a block that is not in the memories. A date, a \
place, a feeling, a name, an outcome nobody wrote. Also a quote that is not \
copied character for character from the block's own text, or a memory_id not \
in the archive.
- contradiction: two people remembering one event differently, merged into one \
account, or one of them dropped. Both must stay, each attributed, with \
diverges set to true.
- structure: chapters divided or ordered against what the memories say — a \
life event split across two chapters, unrelated years forced together, undated \
material not last.
- instruction: the owner asked for the book to be planned a certain way, and \
the draft did not follow it where the memories allowed.
- thin: a memory too short or too empty to compose a paragraph from. Say which \
one, so the owner can add to it.

Each finding is one or two plain sentences, addressed to the owner of the \
memoir, who is not a writer or an engineer. Name the chapter it concerns when \
there is one. Do not quote the memories at length. Do not praise the draft: \
what is right needs no finding.

The corrected plan:

- Return revised only when you would change something. Null means keep the \
draft.
- You may remove a clause, a block or a chapter; re-word a block so that it \
says only what the memories say; split or merge chapters; reorder; restore a \
dropped account with diverges true. You may never add anything the memories \
do not contain, and every block still needs a source for every clause, with \
quote copied exactly from the block's text.
- Set fixed to true on a finding only when the revised plan addresses it.
- Keep every memory_id, asset_id and anchor_memory_id exactly as given."""


# ---------------------------------------------------------------------------
# The guide
# ---------------------------------------------------------------------------
#
# The one agent that talks to the owner. It never sees a memory's words — only
# counts, titles, the reviewer's findings and the builder's reason — so it
# has nothing to fabricate from, and nothing a family wrote reaches a third
# model call. It runs twice: before the builder, to turn the owner's request
# into precise instructions; after, to say in plain words what happened and
# what would make the next plan better.

_GUIDE_SYSTEM = """You are the guide for a family assembling a memoir of someone they have lost. You speak to the owner of the memoir, plainly, in two to four sentences. They are not a writer or an engineer, and they may be grieving.

You are told facts about the archive and about how planning went: how many memories there are, how many are dated, what the reviewer found, why the planner could not run. You say only what follows from those facts. You never invent a memory, a person, a date or an event, and you never describe what the memoir says — you have not read it.

Never give a score, a percentage, a count of what is "missing", or any encouragement to keep going. Say what would help — a date on an undated memory, a second account of an event, more words on a memory that is only a title — and stop.

When the owner has asked for the book to be planned a certain way, restate that request for the planner in instructions_for_builder: precise, short, and adding nothing the owner did not ask for. Leave it null when there was no request. Never address the reader of the memoir, and never write about the family's grief."""


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


# How much room the model gets to write a whole book in.
#
# 65,536 is the output ceiling on the Gemini 3.x flash tier, and asking for all
# of it is right here: this call happens once, by hand, and the failure it
# prevents is silent. Truncation does not arrive as an error — it arrives as a
# JSON document that stops mid-string, which `gemini.generate` now names rather
# than reporting as a malformed plan.
#
# It is a ceiling, not a spend: the model is billed for what it writes.
MAX_PLAN_TOKENS = 65536

PLACEMENTS = ("margin", "inset", "carousel")


def verify_figures(
    figures: list[PlannedFigure],
    allowed_assets: dict,
    cited: set[str],
) -> list[dict]:
    """Turn the model's claimed figures into ones the database can trust.

    The same posture as `verify`: everything the model said is a claim until it
    is checked against the archive, and what cannot be checked is dropped
    rather than guessed at.

    `allowed_assets` maps asset id to the photograph row, built from what we
    actually read out of storage. `cited` is the set of memory ids this
    chapter's prose drew on.

    Four things are refused:

      an unknown asset id      a photograph nobody uploaded. The foreign key
                               would reject it anyway
      an asset in another
      chapter's moment         allowed. The model is choosing where a
                               photograph belongs and is better placed to know
                               than the archive order is
      an unknown anchor        the memory is not in this chapter's prose, so
                               there is no paragraph of it to sit beside.
                               Dropped: a figure anchored into a different part
                               of the life captions it with somebody else's
                               afternoon
      an unknown placement     coerced to 'margin', the safe one. The enum
                               would reject anything else, and the outer
                               column is where a photograph does least harm

    A duplicate asset id is collapsed to the first. One photograph appears in
    the book once.
    """
    verified: list[dict] = []
    seen: set[str] = set()

    for figure in figures:
        asset_id = (figure.asset_id or "").strip()
        if asset_id not in allowed_assets:
            logger.warning("Assembly placed a photograph that is not in the archive")
            continue
        if asset_id in seen:
            continue

        anchor_id = (figure.anchor_memory_id or "").strip()
        if anchor_id not in cited:
            logger.info(
                "Assembly anchored a photograph to a memory this chapter does "
                "not quote"
            )
            continue

        seen.add(asset_id)
        placement = figure.placement if figure.placement in PLACEMENTS else "margin"

        verified.append(
            {
                "asset_id": asset_id,
                "anchor_memory_id": anchor_id,
                "placement": placement,
            }
        )

    return verified


def _clean(
    plan: Plan, memories: list[dict], photographs: list[dict] | None = None
) -> tuple[list[dict], dict]:
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

    **What it counts, and why counting is not optional.** One summary line
    records how much arrived and how much of it was dropped, by reason. It
    carries no text — never a paragraph, never a quote, never a title, for the
    reason `integrations/gemini.py` gives about its own logs. It exists because
    "nothing survived verification" was, for one run, the only thing said about
    a plan that came back with chapters full of empty blocks: the fallback then
    produced a book divided by decade and the cause was unknowable from the
    outside. A block dropped for having no text used to be dropped in silence.
    """
    allowed = {str(m["id"]): m["participant_id"] for m in memories}
    by_id = {str(m["id"]): m for m in memories}
    allowed_assets = {str(a["id"]): a for a in photographs or []}

    chapters: list[dict] = []
    arrived = 0
    blank = 0
    unattributed = 0

    for planned in plan.chapters:
        blocks: list[dict] = []
        cited: set[str] = set()

        for block in planned.blocks:
            arrived += 1
            text = (block.text or "").strip()
            if not text:
                blank += 1
                continue

            sources = verify(text, block.sources, allowed)
            if not sources:
                unattributed += 1
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
                # Checked against the photographs actually read and against
                # this chapter's own citations, so a figure can only anchor to
                # a paragraph that exists in the chapter holding it.
                "figures": verify_figures(planned.figures, allowed_assets, cited),
                # The memories this chapter drew on, in archive order. The
                # writer's photograph pass reads this: a photograph belongs in
                # the chapter that used the memory it came with.
                "memories": [by_id[mid] for mid in by_id if mid in cited],
            }
        )

    # Shape only. No text, ever — see the docstring.
    logger.info(
        "Assembly plan: %d chapters in, %d kept; %d blocks in, %d blank, "
        "%d unattributed",
        len(plan.chapters),
        len(chapters),
        arrived,
        blank,
        unattributed,
    )

    return chapters, {
        "chapters": len(plan.chapters),
        "blocks": arrived,
        "blank": blank,
        "unattributed": unattributed,
    }


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


def _shrink(raw: bytes) -> tuple[str, bytes] | None:
    """One photograph, re-encoded small enough to send. None if unreadable.

    Always re-encoded, never passed through: an original is between two and
    forty megabytes, and the judgement being asked of the model — which moment
    is this, and does it match that paragraph — survives 768px intact. It also
    normalises the format, so a HEIC or a TIFF from somebody's phone becomes
    something Gemini accepts without a per-format branch here.

    `convert("RGB")` before saving because JPEG has no alpha channel and a
    transparent PNG otherwise raises rather than flattening.
    """
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.thumbnail((_MAX_IMAGE_EDGE, _MAX_IMAGE_EDGE))
            buffer = io.BytesIO()
            image.convert("RGB").save(
                buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True
            )
    except (UnidentifiedImageError, OSError, ValueError):
        # A file that is not an image, or is truncated. One unusable
        # photograph is not worth failing a family's whole book over.
        logger.warning("Assembly could not read a photograph; leaving it out")
        return None

    return "image/jpeg", buffer.getvalue()


async def _load_images(
    photographs: list[dict],
) -> tuple[list[tuple[str, bytes]], list[dict]]:
    """The photographs, shrunk and in order, plus the rows that made it.

    Returns the inline parts and the subset of `photographs` they correspond
    to, index for index. Both are needed: the parts go in the request, and the
    rows are what `_clean` will accept an `asset_id` from — so a photograph
    that could not be loaded also cannot be placed by the model, which is the
    correct outcome rather than a coincidence.

    Nothing here raises. Storage being unreachable, an object being missing, a
    file that is not an image, and the request being full are all the same kind
    of answer: this photograph is not in front of the model. The plan is still
    a plan, and `assembly_service` still places every photograph in the book by
    its own rule.
    """
    parts: list[tuple[str, bytes]] = []
    shown: list[dict] = []
    total = 0
    skipped = 0

    for photograph in photographs:
        if total >= _MAX_INLINE_BYTES:
            skipped += 1
            continue

        try:
            raw = await supabase_storage.download_object(
                photograph["storage_path"], max_bytes=_MAX_ASSET_BYTES
            )
        except supabase_storage.StorageError as exc:
            logger.warning("Assembly could not fetch a photograph: %s", exc)
            continue

        shrunk = _shrink(raw)
        if shrunk is None:
            continue

        mime, data = shrunk
        if total + len(data) > _MAX_INLINE_BYTES:
            skipped += 1
            continue

        parts.append((mime, data))
        shown.append(photograph)
        total += len(data)

    if skipped:
        logger.warning(
            "Assembly did not look at %s photographs: the request was full at "
            "%s of them",
            skipped,
            len(shown),
        )

    return parts, shown


def _describe(
    memories: list[dict], memoir: dict, prose, photographs: list[dict] | None = None
) -> str:
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

    # The photographs are labelled here and attached in the same order, so
    # "the third image" and "the third record below" are the same photograph.
    # An inline image part carries no id of its own — this list is the only
    # thing that lets the model name one.
    if photographs:
        lines.append(
            "---\n"
            "The photographs follow, as images, in this order. Each is listed "
            "here with the id to place it by:"
        )
        for position, photograph in enumerate(photographs, start=1):
            lines.append(
                f"image {position}\n"
                f"asset_id: {photograph['id']}\n"
                f"sent with memory_id: {photograph['memory_id']}"
            )

    return "\n\n".join(header + lines)


async def plan(
    memories: list[dict],
    memoir: dict,
    prose,
    photographs: list[dict] | None = None,
    instructions: str | None = None,
) -> Outcome:
    """Build, review, guide. Returns an `Outcome`.

    `instructions` arrive already restated: the only caller that sends any is
    `chat_service`, and the guide's chat turn is what restated them. A second
    restating call here was one flash request per replan spent saying the
    same thing again. The guide runs once, last, to tell the owner what
    happened; if it cannot, the owner reads `reason` instead, as before.
    """
    shape = _shape(memories, memoir)
    outcome = await _draft_and_review(
        memories, memoir, prose, photographs, instructions
    )
    outcome.guide = await _guide_after(shape, outcome)
    return outcome


def _shape(memories: list[dict], memoir: dict) -> dict:
    """The archive as counts. What the guide is allowed to know about it."""
    dated = [m["happened_on"] for m in memories if m.get("happened_on")]
    return {
        "subject": memoir["subject_name"],
        "memories": len(memories),
        "dated": len(dated),
        "earliest": str(min(dated)) if dated else None,
        "latest": str(max(dated)) if dated else None,
        "contributors": len({m["participant_id"] for m in memories}),
    }


async def _guide_after(shape: dict, outcome: Outcome) -> str | None:
    """A plain sentence or three about how planning went. None if it could not."""
    context = {
        "archive": shape,
        "organised_by": "planner" if outcome.chapters is not None else "by_date",
        "reason": outcome.reason,
        "chapters": [c["title"] for c in outcome.chapters or []],
        "review": outcome.review,
    }
    prompt = (
        f"How planning went: {context}\n\n"
        "Write the note to the owner. Leave instructions_for_builder null."
    )
    reply = await _guide(prompt)
    if reply is None:
        return None
    return reply.note.strip()[:1000] or None


async def _guide(prompt: str) -> GuideNote | None:
    try:
        return await gemini.generate(
            prompt,
            GuideNote,
            system=_GUIDE_SYSTEM,
            model=settings.gemini_model,
            timeout=60.0,
        )
    except gemini.GeminiError as exc:
        logger.info("Guide did not run: %s", exc)
        return None


async def _draft_and_review(
    memories: list[dict],
    memoir: dict,
    prose,
    photographs: list[dict] | None,
    instructions: str | None,
) -> Outcome:
    """Read the archive, draft the book, review the draft. Returns an `Outcome`.

    `Outcome.chapters` is None when the deterministic assembler should run, and
    `Outcome.reason` is then the sentence the owner reads. It used to live only
    in the log, which the owner cannot see, so "the archive could not be read
    this time" was all they got.

    Two model calls. The builder drafts on the assembly model; the reviewer
    reads that draft against the archive on the cheaper one and returns
    findings plus, if it would change anything, a revision. The revision is
    checked exactly as the draft was and taken only if it attributes at least
    as well — see `_accept_revision`. A reviewer that fails costs nothing but
    its findings: the draft stands.

    `instructions` are the owner's request about how the book should be
    divided — "keep the war years together", "one chapter per house" — as
    the guide restated it from the conversation. They shape the plan; they
    cannot add to it, because the attribution rules still hold over every
    paragraph.

    None means "use the deterministic assembler" and covers every failure:
    switched off, no key, unreachable, refused, malformed, or a plan that
    survived verification with nothing left in it. The caller falls back rather
    than raising, because a memoir that cannot be assembled at all is a worse
    outcome than one assembled by decade.

    Runs on `gemini_assembly_model` — the slower tier. This call happens once,
    by hand, over an entire archive, and the judgement it needs is the whole
    reason it exists.
    """
    # Fetched before the prompt is built, because what the prompt may name is
    # exactly what was successfully fetched.
    images, shown = await _load_images(photographs or [])
    described = _describe(memories, memoir, prose, shown)
    if instructions and instructions.strip():
        described += (
            "\n\n---\nThe owner asks that the book be planned this way. Follow "
            "it where the memories allow, and never invent anything to satisfy "
            f"it:\n{instructions.strip()[:2000]}"
        )

    try:
        result = await gemini.generate(
            described,
            Plan,
            system=_SYSTEM,
            model=settings.gemini_assembly_model,
            images=images,
            # Lower than the follow-up questions use. This is composition
            # bounded by evidence — every clause has to be quotable back to a
            # memory — and a higher temperature buys invention, which is the
            # one thing forbidden here.
            temperature=0.3,
            # Minutes, not seconds. It is reading a whole family archive and
            # writing a book out of it, and nobody is watching a spinner: the
            # owner pressed a button and expects to wait.
            timeout=300.0,
            # And spend some of that budget on the model being busy. The
            # default schedule gives up after four seconds of waiting, which
            # turned a transient "high demand" 503 into a book divided by
            # decade with five minutes of patience unspent.
            backoff=gemini.PATIENT_BACKOFF,
            # Room to finish. A plan is longer than the archive it came from —
            # every paragraph, plus a verbatim quote for every source in it —
            # and on the 3.x models the thinking tokens come out of the same
            # allowance. Left at the model's default, a fifteen-memory archive
            # came back as JSON that stopped mid-string, three runs out of
            # four, and the decade fallback ran each time.
            max_output_tokens=MAX_PLAN_TOKENS,
        )
    except gemini.GeminiDisabled as exc:
        # Warning, not info. This is the line that answers "is the model
        # actually running in this deployment" — an environment with no
        # GEMINI_API_KEY plans every memoir by decade and is otherwise silent
        # about it, and that went unnoticed once.
        logger.warning("Assembly is planning by date: %s", exc)
        return Outcome(None, "The model is switched off in this deployment.")
    except gemini.GeminiError as exc:
        logger.warning("Assembly is planning by date: %s", exc)
        return Outcome(None, f"The model could not be reached: {exc}")

    chapters, tally = _clean(result, memories, shown)
    if not chapters:
        logger.warning("Assembly is planning by date: nothing survived verification")
        return Outcome(None, _nothing_survived(memories, tally))

    review = await _review(result, described)
    if review is None:
        return Outcome(chapters)

    revised = False
    if review.revised is not None:
        candidate, candidate_tally = _clean(review.revised, memories, shown)
        if _accept_revision(tally, candidate, candidate_tally):
            chapters, revised = candidate, True
        else:
            logger.info("Reviewer's revision refused; keeping the draft")

    return Outcome(chapters, review=_findings(review, revised))


async def _review(draft: Plan, described: str) -> Review | None:
    """The reviewer's reading of the draft, or None if it could not read.

    Text only — no images. Whether a photograph sits beside the right
    paragraph is already checked deterministically by `verify_figures`, and
    what the reviewer is for is the prose: what it claims, and who said it.
    """
    prompt = (
        f"{described}\n\n---\nThe draft plan, as JSON:\n"
        f"{draft.model_dump_json(exclude_none=True)}"
    )
    try:
        return await gemini.generate(
            prompt,
            Review,
            system=_REVIEW_SYSTEM,
            model=settings.gemini_model,
            temperature=0.2,
            timeout=300.0,
            backoff=gemini.PATIENT_BACKOFF,
            # A revision is a whole plan, so it needs the same room.
            max_output_tokens=MAX_PLAN_TOKENS,
        )
    except gemini.GeminiError as exc:
        logger.warning("Reviewer did not run: %s", exc)
        return None


def _accept_revision(
    draft_tally: dict, candidate: list[dict], candidate_tally: dict
) -> bool:
    """Whether the reviewer's plan replaces the draft.

    The reviewer is asked to remove and re-word, never to add — but it is a
    model, and a re-worded paragraph can lose the quote that attributed it.
    So the rule is the one thing this file can measure: a revision is taken
    only if it left at least one chapter standing and dropped no more blocks
    for want of attribution than the draft did.
    """
    return bool(candidate) and (
        candidate_tally["unattributed"] <= draft_tally["unattributed"]
    )


def _findings(review: Review, revised: bool) -> dict:
    """The review as stored on the plan: shape checked, content trimmed."""
    findings = []
    for finding in review.findings[:MAX_FINDINGS]:
        note = finding.note.strip()
        if not note:
            continue
        findings.append(
            {
                "kind": finding.kind if finding.kind in FINDING_KINDS else "structure",
                "chapter": (finding.chapter or "").strip()[:200] or None,
                "note": note[:600],
                # A finding cannot have been fixed by a revision that was
                # not taken.
                "fixed": bool(finding.fixed) and revised,
            }
        )
    logger.info("Reviewer: %d findings, revision %s", len(findings), "taken" if revised else "not taken")
    return {"findings": findings, "revised": revised}


def _nothing_survived(memories: list[dict], tally: dict) -> str:
    """Why a plan that came back was thrown away, in the owner's terms."""
    read = len(memories)
    if tally["blocks"] == 0:
        return (
            f"The model read {read} memor{'y' if read == 1 else 'ies'} and "
            "found nothing it could write a paragraph from. Short or "
            "placeholder memories give it nothing to compose with."
        )
    return (
        f"The model wrote {tally['blocks']} paragraphs, but "
        f"{tally['unattributed']} could not be traced back to a memory word "
        f"for word and {tally['blank']} were empty, so none were kept."
    )


# ---------------------------------------------------------------------------
# The guide, in conversation
# ---------------------------------------------------------------------------


class OutlineChapter(BaseModel):
    """One chapter as the guide would have it: which one, and its title."""

    number: int = Field(description="the chapter's current number in the outline")
    title: str


class ChatTurn(BaseModel):
    """One reply from the guide, and what it wants done to the book."""

    reply: str
    replan: bool = Field(
        default=False,
        description=(
            "true only when the owner asked for something that changes which "
            "chapter a memory belongs in, and the planner must run again"
        ),
    )
    instructions_for_builder: str | None = Field(
        default=None,
        description="Set when replan is true: the owner's request, restated.",
    )
    outline: list[OutlineChapter] | None = Field(
        default=None,
        description=(
            "Set only to rename, reorder or leave out chapters: every chapter "
            "to keep, in reading order, by its current number, with the title "
            "it should have. Null when the outline is not being changed."
        ),
    )


_CHAT_ADDENDUM = """

You are now in conversation with the owner. You are given the shape of the \
archive, the outline as it stands — numbered chapters with their titles, the \
reviewer's findings, the guide's last note — and the conversation so far, \
ending with the owner's latest message. Reply to that message.

You can change the book two ways, and you should do so when asked, as well \
as suggest it when the outline or the findings show something worth fixing:

  outline   to rename, reorder or leave out chapters. List every chapter to \
            keep, in the order wanted, by its current number, with its title. \
            A chapter left off the list is dropped. Numbers are the ones in \
            the outline you were shown — never invent one.
  replan    with instructions_for_builder, when the change is to how the \
            memories are divided: what goes together, what is separate, what \
            the book is organised around. This takes minutes and replaces \
            the outline; say so in one clause.

Either way the book is rebuilt from the result straight away. When you have \
made a change, say plainly what you did. When the owner's message is not a \
request — a question, or a worry — answer it, and where the findings or the \
archive's shape suggest one, recommend one concrete change and ask whether \
they want it. If a request could be read two ways, or the archive as \
described cannot honour it — a chronological order when some memories have \
no date, for instance — ask one short question first and change nothing. \
When the owner's latest message answers a question you asked, act on it in \
that turn. Never set both outline and replan: a replan produces new chapters \
and the numbers no longer mean anything. You have not planned anything when \
you reply: never describe what a new outline holds. That is told to the \
owner afterwards, by the note written when planning finishes."""


async def chat(
    shape: dict, plan: dict | None, history: list[dict], message: str
) -> ChatTurn | None:
    """The guide's reply to the owner's latest message. None if it could not.

    `plan` is the stored plan's summary, reduced here to what the guide may
    see: numbered titles, the review, the note. Never a block. The numbers
    are what an `outline` answer refers to, and `chat_service` maps them
    back to chapter ids — a model is not handed uuids to copy. `history`
    is the prior messages as `{role, body}`, oldest first.
    """
    outline = None
    if plan is not None:
        outline = {
            "organised_by": plan["organised_by"],
            "chapters": [
                {"number": number, "title": c["title"]}
                for number, c in enumerate(plan["chapters"], start=1)
            ],
            "review": plan.get("review"),
            "guide": plan.get("guide"),
        }
    transcript = "\n".join(
        f"{turn['role']}: {turn['body'][:2000]}" for turn in history[-20:]
    )
    prompt = (
        f"The archive: {shape}\n\nThe outline: {outline}\n\n"
        f"The conversation so far:\n{transcript}\n\nowner: {message.strip()[:4000]}"
    )
    try:
        return await gemini.generate(
            prompt,
            ChatTurn,
            system=_GUIDE_SYSTEM + _CHAT_ADDENDUM,
            model=settings.gemini_model,
            timeout=60.0,
        )
    except gemini.GeminiError as exc:
        logger.info("Guide did not answer: %s", exc)
        return None
