# Pydantic models = the request/response "shape" for the finished memoir.
#
# `response_model` matters more here than anywhere else in the API, because the
# audience is the widest the product has: anyone holding a view link. That is
# family, and it is also whoever the link was forwarded to. A field declared
# here can reach them; a field not declared here cannot, no matter what the SQL
# selected.
#
# One field is missing on purpose and should stay missing. `memoir.never_forget`
# is the owner's private answer to "what should never be forgotten about them",
# and `LinkInvitation` in memoir_models.py already excludes it for the same
# reason: a link gets forwarded, and forwarding it would forward that too.

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from src.models.memory_models import MemoryKind

# The `block_kind` enum from migration 0011, mirrored. A Literal rather than a
# free string so a malformed value is caught at the edge with a 422 naming the
# field, instead of reaching Postgres and returning as a less legible 22P02.
BlockKind = Literal["paragraph", "pull", "figure"]

# The `figure_placement` enum. Chosen by the assembly step, never by a client —
# it is on the response models only.
FigurePlacement = Literal["margin", "inset"]


class BlockSource(BaseModel):
    """One person's memory, and which words of the paragraph came from it.

    The model the "never fabricate" rule is written in. A chapter is assembled
    and rephrased from many contributions, so a reader must be able to ask of
    any clause who actually said it — and get a name, a medium and a date back.

    `start_offset` / `end_offset` are character offsets into the block's own
    text, half-open. Both null means the whole block came from this source.

    `diverges` marks a source that contradicts the assembled prose and was kept
    anyway. Both accounts are shown; neither is corrected.
    """

    id: UUID
    memory_id: UUID
    participant_id: UUID

    # Who left it. Resolved from memoir_participant so the reader can print a
    # credit line without joining anything itself.
    name: str
    relationship: str

    # How it was given — the memory's own `kind`, which the backend already
    # derives in `_derive_kind`. Not a second vocabulary invented here: "voice"
    # in a credit line and "voice" in the archive have to mean one thing.
    medium: MemoryKind

    # The year the credit line prints. The date the memory is *about* when
    # somebody could say, otherwise the year it was given. Null for neither,
    # which cannot happen, but the reader copes rather than assuming.
    year: int | None

    # The longest recording on that memory, so a voice credit can read "0:47".
    # Null on anything that is not a recording.
    duration_ms: int | None

    start_offset: int | None
    end_offset: int | None
    diverges: bool


class Figure(BaseModel):
    """A photograph on the page, and everything needed to caption it.

    `url` is a freshly signed, expiring link, exactly as `MediaAsset.url` is —
    the bucket is private and this is the only way its contents are readable.
    `storage_path` is deliberately absent here too.

    The caption is **not** written by the assembly step. It is what the person
    who gave the photograph said about it: the memory's title, or the caption
    they typed under it. Inventing a description of somebody's photograph is
    precisely what "never fabricate" forbids, and the honest caption is already
    in the archive.
    """

    asset_id: UUID
    url: str | None
    placement: FigurePlacement

    # The paragraph this belongs beside. A margin plate is positioned against
    # it; an inset sits in the flow but still says what it illustrates.
    anchor_block_id: UUID

    caption: str | None
    credit: str | None
    credit_participant_id: UUID | None
    year: int | None


class Block(BaseModel):
    """One paragraph, one pulled line, or one photograph.

    `text` and `figure` are exclusive by construction — the database's
    `block_prose_shape` and `block_figure_shape` CHECKs enforce it — but both
    are declared optional here because a client reading this model has to
    branch on `kind` anyway.
    """

    id: UUID
    ordinal: int
    kind: BlockKind
    text: str | None = None
    figure: Figure | None = None
    sources: list[BlockSource] = Field(default_factory=list)


class Comment(BaseModel):
    """One thing one person said about a passage.

    Attributed to a `memoir_participant`, never a `user_account`. The people
    reading a finished memoir arrived by link and have no account, which is the
    product's first constraint and applies here exactly as it does to memories.
    """

    id: UUID
    participant_id: UUID
    name: str
    relationship: str
    body: str
    created_at: datetime
    is_owner: bool


class CommentThread(BaseModel):
    """A conversation about one passage, oldest comment first.

    Anchored the same way a source is — a block, and optionally a range inside
    it — so the reader positions both with one piece of arithmetic. Null
    offsets mean the thread is about the whole paragraph.
    """

    id: UUID
    chapter_id: UUID
    block_id: UUID
    start_offset: int | None
    end_offset: int | None
    resolved_at: datetime | None
    comments: list[Comment] = Field(default_factory=list)


class ChapterSummary(BaseModel):
    """One line of the contents rail.

    Title and years, and nothing else. The rail is the whole book at a glance,
    so this is deliberately cheap enough to send every chapter of.
    """

    id: UUID
    ordinal: int
    title: str
    from_year: int | None
    through_year: int | None


class Chapter(BaseModel):
    """One chapter in full: the blocks, their sources, and the conversation."""

    id: UUID
    memoir_id: UUID
    ordinal: int
    title: str
    from_year: int | None
    through_year: int | None

    blocks: list[Block] = Field(default_factory=list)
    threads: list[CommentThread] = Field(default_factory=list)

    # Everyone whose memories went into this chapter, most-cited first. The
    # closing line of a chapter reads "Told by Margaret Reyes, Thomas Marsh and
    # four others", and this is what it counts.
    told_by: list[str] = Field(default_factory=list)
    memory_count: int


class ReaderPerson(BaseModel):
    """One row of the back matter's index of people."""

    participant_id: UUID
    name: str
    relationship: str
    memory_count: int


class ReaderTotals(BaseModel):
    """The colophon's four numbers.

    Facts about what the book is made of, not a measure of anything. The
    product forbids progress indicators and this is not one — there is no
    denominator.
    """

    memories: int
    people: int
    chapters: int
    recordings: int


class MemoirReading(BaseModel):
    """The book's covers: who it is about, its contents, and its colophon.

    One response rather than three, because the reader's first screen renders
    the title page, the contents rail and the back matter from it, and three
    round trips to build one page is three chances to show half a book.
    """

    memoir_id: UUID
    subject_name: str
    born_year: int | None
    through_year: int | None
    subject_is_living: bool | None

    # Null while the memoir is still a draft. The reader shows an owner their
    # unpublished book; it says so rather than pretending it is finished.
    published_at: datetime | None

    chapters: list[ChapterSummary] = Field(default_factory=list)
    people: list[ReaderPerson] = Field(default_factory=list)
    totals: ReaderTotals


class CommentCreate(BaseModel):
    """A comment being left on a published memoir.

    Two shapes in one model, distinguished by which id is present:

      `block_id`   start a new thread about that block, optionally about a
                   range of characters inside it
      `thread_id`  reply to a conversation that already exists

    Exactly one, validated below. Accepting both would leave the server
    choosing which the caller meant, and it would sometimes choose wrong.
    """

    body: str = Field(..., min_length=1, max_length=4000)

    block_id: UUID | None = None
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, gt=0)
    thread_id: UUID | None = None

    # Who this is from. Required for a caller holding a view link, ignored for
    # the owner — they are already identified by their token, and their owner
    # participant row already carries their name. The route enforces that
    # difference; the model cannot see which credential arrived.
    display_name: str | None = Field(default=None, min_length=1, max_length=120)

    # Returned after the first comment and sent back on every later one, so a
    # returning reader is recognised as the same person rather than appearing
    # in the memoir twice. The same token a contributor already holds.
    participant_token: str | None = None

    @model_validator(mode="after")
    def _one_target(self):
        if (self.block_id is None) == (self.thread_id is None):
            raise ValueError("send exactly one of block_id or thread_id")

        if self.thread_id is not None and (
            self.start_offset is not None or self.end_offset is not None
        ):
            raise ValueError("a reply cannot re-anchor its thread")

        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("send both offsets or neither")

        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset <= self.start_offset
        ):
            raise ValueError("end_offset must be after start_offset")

        return self


class CommentReceipt(BaseModel):
    """What a reader gets back after commenting.

    The thread as it now stands, plus the token that makes them the same person
    next time. `participant_token` is null for the owner, who has a real
    account and must not be handed a second, weaker credential.
    """

    thread: CommentThread
    participant_token: str | None
