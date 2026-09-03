# Pydantic models = the API's contract. What may come in, and what goes out.
#
# The outgoing models matter most on this feature, because two very different
# callers read from it: the owner, who may see everything in their memoir, and
# a contributor, who may see only what they themselves added. `response_model`
# on the routes is what enforces that — a field absent from the model cannot
# reach the client no matter what the SQL selected.

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# The memory_kind enum from migration 0003, mirrored. A Literal rather than a
# free string so FastAPI rejects "audio" or "Voice" at the edge with a 422,
# before it reaches Postgres and comes back as a less legible 22P02.
MemoryKind = Literal["text", "photo", "voice"]

# The asset_kind enum. Coarser than MemoryKind on purpose: a photo memory holds
# images, a voice memory holds audio.
AssetKind = Literal["image", "audio"]


class MemoryCreate(BaseModel):
    """A new memory, written by the owner from `/archive/new`.

    Note there is no `kind`. A memory can hold writing, photographs and
    recordings all at once — which is how people actually remember an
    afternoon — so which of those it *is* stopped being a choice and became a
    description the backend derives from the contents. See `_derive_kind` in
    `domain/memories/memory_service.py`.
    """

    title: str | None = Field(default=None, max_length=200)
    body_text: str | None = None
    happened_on: date | None = None

    # Assets uploaded before the memory existed, now being adopted by it. The
    # upload has to come first — a file needs somewhere to go before there is a
    # row to attach it to — so the client sends the ids it was given back.
    asset_ids: list[UUID] = Field(default_factory=list)


class MemoryUpdate(BaseModel):
    """A partial edit. Every field optional; unset fields are left alone.

    Routes must use `model_dump(exclude_unset=True)` on this, or a client
    sending only a title would blank the body text.
    """

    title: str | None = None
    body_text: str | None = None
    happened_on: date | None = None


class AssetAttachment(BaseModel):
    """Files being added to a memory that already exists.

    A separate model from `MemoryUpdate` rather than an `asset_ids` field on
    it, because the two are different kinds of operation. An update is
    partial — say nothing about a field and it is left alone. A list of assets
    on a partial update would have to mean one of two things, and both are
    wrong: either it can only ever add (so there is no way to remove), or it
    replaces the set (so a client that forgets to send the full list silently
    deletes somebody's photographs).

    Removing is a DELETE against one asset, which is the shape that matches
    what it does.
    """

    asset_ids: list[UUID] = Field(..., min_length=1)


class ContributedMemory(BaseModel):
    """A memory arriving through a share link, from someone with no account.

    Carries the contributor's own name, because there is no account to look it
    up from. That is the product working as designed, not a shortcut.

    No `kind`, for the same reason as `MemoryCreate`: a contributor can leave a
    recording, a photograph and a note together, and the backend works out what
    to call it.
    """

    title: str | None = Field(default=None, max_length=200)
    body_text: str | None = None
    happened_on: date | None = None
    asset_ids: list[UUID] = Field(default_factory=list)

    # Who this is from. Required every time, and honoured every time: a
    # returning contributor who sends a different name is renamed.
    #
    # Because the name lives on the participant rather than on each memory,
    # that renames the memories they have already left, too. Deliberate — it is
    # the same person and this is their name — and the contributor screen says
    # so before they change it. See `resolve_participant` in
    # `domain/memories/memory_service.py`.
    display_name: str = Field(..., min_length=1, max_length=120)

    # Returned to the browser after the first contribution and sent back on
    # every later one, so a returning contributor is recognised as the same
    # person instead of appearing in the archive twice.
    participant_token: str | None = None


# Where a transcript job has got to. Mirrors the `transcript_status` enum from
# migration 0006. 'skipped' is not a failure — it means transcription was
# switched off, so nothing went wrong and nothing should be retried.
TranscriptStatus = Literal["queued", "processing", "done", "failed", "skipped"]


class TranscriptSegment(BaseModel):
    """One paragraph of a transcript, with where it falls in the recording.

    Milliseconds, matching `media_asset.duration_ms`. These exist so a
    transcript reads as paragraphs rather than a wall of prose, and so a
    paragraph can later be clicked to seek. Word-level timings are deliberately
    not stored — see the note at the top of `integrations/assemblyai.py`.
    """

    start: int | None
    end: int | None
    text: str


class Transcript(BaseModel):
    """What was said in a recording, as the frontend sees it.

    Note what is missing. `provider_id` is AssemblyAI's job id — an internal
    handle, and the credential the webhook is addressed by; handing it out
    invites someone to go asking AssemblyAI about it directly. `error` is the
    provider's own message, which is for the log: a family does not need to be
    told a machine struggled with their grandmother's accent, and the audio is
    unaffected either way.

    `response_model` filtering is what keeps both out, even if a future query
    selects them by accident. It already does.
    """

    status: TranscriptStatus
    # Null while queued or processing, and on failure. The frontend keys off
    # `status`, not off this being present.
    text: str | None = None
    segments: list[TranscriptSegment] | None = None
    # What the provider detected. Not assumed: these memoirs are not all in
    # English, and a hardcoded language returns confident nonsense.
    language_code: str | None = None
    confidence: float | None = None


class MediaAsset(BaseModel):
    """One stored object, as the frontend sees it.

    `url` is a freshly signed, expiring link — never a permanent address. The
    bucket is private, and this is the only way its contents are readable.

    `storage_path` is deliberately absent. It is an internal address; handing
    it out invites a client to build its own URLs against it.
    """

    id: UUID
    kind: AssetKind
    mime_type: str
    byte_size: int
    duration_ms: int | None
    url: str | None

    # Audio only, and null even then until a job has been created for it.
    # A recording without a transcript is still a recording — every reader of
    # this field has to cope with None.
    transcript: Transcript | None = None


class Memory(BaseModel):
    """A memory, with who left it and whatever they attached."""

    id: UUID
    memoir_id: UUID
    kind: MemoryKind
    title: str | None
    body_text: str | None
    happened_on: date | None
    created_at: datetime

    # Resolved from memoir_participant, so the archive can say who each memory
    # came from without the frontend joining anything itself.
    contributor_name: str
    contributor_relationship: str

    # Which participant left it, and whether that participant is the owner.
    #
    # The id is what lets the owner's contributors screen show one person's
    # memories together — grouping on `contributor_name` would merge two people
    # who happen to share a name, which is exactly the confusion the merge flow
    # exists to sort out.
    #
    # `is_owner` is a real field rather than a name comparison for the same
    # reason. A contributor who types the owner's name is not the owner.
    #
    # Note this model is also the response for `GET /j/{token}/memories`, so a
    # contributor sees both. Harmless: the id is their own row, which they
    # already hold a token for, and `is_owner` is false for every memory they
    # can see. Anything genuinely owner-only needs a separate model — see the
    # note at the top of `api/memories.py`.
    participant_id: UUID
    is_owner: bool

    assets: list[MediaAsset] = Field(default_factory=list)


class ContributionReceipt(BaseModel):
    """What a contributor gets back after leaving a memory.

    The memory itself, plus the token that makes them the same person next
    time. This is the only response in the API that hands out a credential, and
    it goes to someone who by design has no other one.
    """

    memory: Memory
    participant_token: str


class UploadRequest(BaseModel):
    """Ask for permission to upload one file."""

    memoir_id: UUID
    kind: AssetKind
    mime_type: str = Field(..., max_length=120)
    original_filename: str | None = Field(default=None, max_length=255)

    # Milliseconds, from the browser's MediaRecorder. Audio only — the schema
    # rejects it on an image.
    duration_ms: int | None = Field(default=None, gt=0)


class UploadTicket(BaseModel):
    """Where to PUT the file, and what to call the asset afterwards.

    `upload_url` works once and only for this asset's path. The client PUTs the
    bytes there, then calls the confirm endpoint with `asset_id`.
    """

    asset_id: UUID
    upload_url: str


class StorageUsage(BaseModel):
    """Bytes held against bytes allowed, for the billing screen.

    A measure of how full a container is — not a completion percentage. The
    product forbids progress indicators, and this is not one.
    """

    used_bytes: int
    limit_bytes: int
