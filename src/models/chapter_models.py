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

    # No name here, and none is accepted. Who this is from was settled at the
    # door: the owner by their account, a reader by the session they were given
    # in exchange for the passphrase and their name. A `display_name` on this
    # model would be a second, weaker way to claim to be somebody.

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
    """What a reader gets back after commenting: the thread as it now stands.

    It used to carry a `participant_token` as well — the thing that made
    somebody the same person next time. That is issued at the door now, by
    `POST /r/{token}/open`, along with everything else about who they are.
    """

    thread: CommentThread


class AssemblyResult(BaseModel):
    """What assembling an archive into chapters actually produced.

    Four counts and nothing else. The owner is about to be told a number and it
    should be the real one — "8 chapters, 41 paragraphs" is a fact about their
    archive, not a score, and it is deliberately not a percentage of anything.
    A memoir has no denominator.

    `figures` can legitimately be lower than the number of photographs in the
    archive: a photograph in a chapter with no prose has no paragraph to sit
    beside, and the schema will not store one without an anchor.
    """

    chapters: int
    blocks: int
    sources: int
    figures: int


class ReaderOpen(BaseModel):
    """Body of POST /r/{token}/open — the door to a published memoir.

    Every field is optional here and the route decides which are required,
    because two very different people knock at this door. The owner arrives
    with a bearer token and needs none of it. Everybody else needs the
    passphrase and a name.

    `relationship` is free text — "Granddaughter", "Cousin David's wife" — and
    not the `relationship_group` enum the onboarding flow collects. A reader is
    describing themselves in a sentence, not choosing from a list, and it is
    stored in `relationship_label` for exactly that reason.
    """

    passphrase: str | None = Field(default=None, max_length=256)
    display_name: str | None = Field(default=None, max_length=120)
    relationship: str | None = Field(default=None, max_length=120)

    # Sent by a browser that has one from contributing months ago, so the
    # person reading is recognised as the person who sent the memories rather
    # than appearing in the memoir twice.
    participant_token: str | None = None


class ReaderSession(BaseModel):
    """What the door gives back.

    `reader_token` goes in `X-Reader-Token` on every request after this one. It
    is a signature rather than a stored row (see `reader_gate.py`), and it stops
    working the moment the passphrase is replaced or the link is revoked.

    What is deliberately absent: anything about the memoir. This route proves a
    credential; `GET /r/{token}` returns the book.
    """

    reader_token: str
    display_name: str
    is_owner: bool

    # Null for the owner, whose participant row is forbidden from carrying one
    # (migration 0003). For everybody else it is the same token the contribute
    # side uses, so one browser is one person across both.
    participant_token: str | None


class SearchHit(BaseModel):
    """One thing a search found, in whichever of the four places it lives.

    One model rather than four, because "where is that story" is one question
    and the person asking does not know which kind of thing they are looking
    for. The fields that do not apply to a kind are null: a photograph has no
    chapter, a paragraph has no single person to credit.

    `excerpt` carries the match wrapped in two control characters rather than
    in `<mark>`. It is built from text a reader typed, and HTML in it would
    mean the frontend rendering user input as markup — see `search_service.py`.
    """

    kind: Literal["chapter", "photo", "recording", "reflection"]

    # The row that matched: a block, a memory, or a comment.
    id: UUID

    # Where to go when it is clicked. Null on a photograph or a recording that
    # is not in a chapter — those live in the archive rather than in the book.
    chapter_id: UUID | None

    title: str
    year: int | None
    excerpt: str

    # Who wrote or gave it. Null on a chapter paragraph, which is assembled
    # from several people and carries its credits on the page itself.
    attribution: str | None


class SearchResults(BaseModel):
    """What a search returns, with the counts the filters are drawn from.

    `counts` comes from the same rows as `hits`, so the number on a filter can
    never disagree with what filtering by it shows — which they would, briefly
    and inexplicably, if somebody left a comment between two queries.
    """

    query: str
    total: int
    counts: dict[str, int]
    hits: list[SearchHit]
