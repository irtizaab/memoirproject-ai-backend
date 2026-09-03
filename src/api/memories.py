# API layer = FastAPI routes. Read the request, call domain/, pick a status.
#
# Two audiences share this file, and the split between them is the product's
# central promise:
#
#   /memoirs/{id}/memories   the owner. Bearer token. Sees everything.
#   /j/{token}/memories      a contributor. No account, ever. Sees only their
#                            own contributions.
#
# They live together because they are the same resource seen from two sides,
# and keeping them adjacent makes it obvious when one grows a field the other
# should not be able to read.

import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from uuid import UUID

from src.api.dependencies import CurrentUser, current_user
from src.domain.memories.memory_service import (
    EmptyMemory,
    MemoirPublished,
    attach_assets,
    contribute_memory,
    create_memory,
    delete_memory,
    get_memory,
    list_contributions,
    list_memories,
    remove_asset,
    update_memory,
)
from src.domain.transcripts.transcript_service import refresh_pending
from src.models.memory_models import (
    AssetAttachment,
    ContributedMemory,
    ContributionReceipt,
    Memory,
    MemoryCreate,
    MemoryUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["memories"])

# Raised in four handlers below. Written once so the wording of the product's
# hardest rule cannot drift between endpoints.
_PUBLISHED = "this memoir is published, and published memoirs cannot change"

# Same reasoning: one sentence, both audiences.
_EMPTY = "a memory needs something in it — writing, a photograph, or a recording"


# ---------------------------------------------------------------------------
# The owner
# ---------------------------------------------------------------------------


@router.get("/memoirs/{memoir_id}/memories", response_model=list[Memory])
def get_memories(memoir_id: UUID, user: CurrentUser = Depends(current_user)):
    """Every memory in the memoir, newest first.

    404 covers both "no such memoir" and "not yours", deliberately. Answering
    403 for the second would confirm the id belongs to a real memoir, which is
    a small leak but a free one to avoid.
    """
    memories = list_memories(str(memoir_id), user.id)
    if memories is None:
        raise HTTPException(status_code=404, detail="memoir not found")

    # Chase any transcript still in flight, outside the transaction that just
    # closed. Costs nothing when there is nothing pending — which, once the
    # webhook is reachable, is nearly always. On a laptop it is the only way a
    # transcript ever arrives.
    return refresh_pending(memories)


@router.get("/memories/{memory_id}", response_model=Memory)
def get_one_memory(memory_id: UUID, user: CurrentUser = Depends(current_user)):
    """One memory in full, for the detail page.

    404 for both "no such memory" and "not yours", as everywhere else — the API
    does not confirm that a stranger's memory exists.

    Transcripts are reconciled here too, so opening a memory whose recording is
    still being written out picks the result up rather than waiting for the
    archive list to be visited again.
    """
    memory = get_memory(str(memory_id), user.id)
    if memory is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return refresh_pending([memory])[0]


@router.post(
    "/memoirs/{memoir_id}/memories", response_model=Memory, status_code=201
)
def post_memory(
    memoir_id: UUID,
    body: MemoryCreate,
    user: CurrentUser = Depends(current_user),
):
    """Record a memory the owner wrote themselves.

    `kind` is not in the request body. A memory may hold writing, photographs
    and recordings together, so what it *is* is derived from what it holds —
    see `_derive_kind`.
    """
    try:
        memory = create_memory(str(memoir_id), user.id, body.model_dump())
    except EmptyMemory:
        raise HTTPException(status_code=400, detail=_EMPTY)
    except MemoirPublished:
        raise HTTPException(status_code=409, detail=_PUBLISHED)

    if memory is None:
        raise HTTPException(status_code=404, detail="memoir not found")
    return memory


@router.patch("/memories/{memory_id}", response_model=Memory)
def patch_memory(
    memory_id: UUID,
    body: MemoryUpdate,
    user: CurrentUser = Depends(current_user),
):
    """Edit a memory that has not been published yet.

    `exclude_unset=True` is what keeps this a partial update: a request sending
    only `title` leaves `body_text` alone, rather than overwriting it with the
    None that Pydantic would otherwise supply as a default.
    """
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")

    try:
        memory = update_memory(str(memory_id), user.id, fields)
    except EmptyMemory:
        # Clearing the words off a memory that holds nothing else. Same rule
        # and same sentence as creating one — the edit is rolled back.
        raise HTTPException(status_code=400, detail=_EMPTY)
    except MemoirPublished:
        raise HTTPException(status_code=409, detail=_PUBLISHED)

    if memory is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return memory


@router.delete("/memories/{memory_id}", status_code=204)
def remove_memory(memory_id: UUID, user: CurrentUser = Depends(current_user)):
    """Delete a memory and the files it held.

    204 with no body: there is nothing meaningful to return about something
    that no longer exists.
    """
    try:
        deleted = delete_memory(str(memory_id), user.id)
    except MemoirPublished:
        raise HTTPException(status_code=409, detail=_PUBLISHED)

    if not deleted:
        raise HTTPException(status_code=404, detail="memory not found")


@router.post("/memories/{memory_id}/assets", response_model=Memory)
def post_memory_assets(
    memory_id: UUID,
    body: AssetAttachment,
    user: CurrentUser = Depends(current_user),
):
    """Add photographs or recordings to a memory that already exists.

    The upload comes first, as it does when creating a memory — a file needs
    somewhere to go before there is anything to attach it to — so the client
    sends the asset ids it was handed back.

    Ids that do not belong to this memoir, or that already belong to another
    memory, simply match nothing. The response is the memory as it now stands,
    so the client never has to guess what changed.
    """
    try:
        memory = attach_assets(
            str(memory_id), user.id, [str(asset_id) for asset_id in body.asset_ids]
        )
    except MemoirPublished:
        raise HTTPException(status_code=409, detail=_PUBLISHED)

    if memory is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return memory


@router.delete("/memories/{memory_id}/assets/{asset_id}", response_model=Memory)
def delete_memory_asset(
    memory_id: UUID,
    asset_id: UUID,
    user: CurrentUser = Depends(current_user),
):
    """Remove one photograph or recording, and delete the file behind it.

    Returns the memory rather than 204, unlike deleting a whole memory: what
    is left still exists and its `kind` may have just changed, so there is
    something meaningful to say.

    400 when this would empty the memory — the same rule and the same sentence
    as creating one, because "a memory needs something in it" does not stop
    applying after the first save. Nothing is deleted in that case.
    """
    try:
        memory = remove_asset(str(memory_id), user.id, str(asset_id))
    except EmptyMemory:
        raise HTTPException(status_code=400, detail=_EMPTY)
    except MemoirPublished:
        raise HTTPException(status_code=409, detail=_PUBLISHED)

    if memory is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return memory


# ---------------------------------------------------------------------------
# The contributor
# ---------------------------------------------------------------------------


@router.post(
    "/j/{token}/memories", response_model=ContributionReceipt, status_code=201
)
def post_contribution(token: str, body: ContributedMemory):
    """Leave a memory through a share link, with no account.

    No `Depends(current_user)`, and that is the product working as designed.
    The token in the path is the entire credential, and it is checked for four
    things at once in `contributable_memoir`: that it exists, has not been
    revoked, grants contribute rather than view, and belongs to a memoir still
    open to new material.

    The response carries a `participant_token`. It is the only credential this
    API ever issues, and it exists so that someone who comes back tomorrow is
    recognised as the same person rather than appearing in the archive twice.
    """
    try:
        result = contribute_memory(token, body.model_dump())
    except EmptyMemory:
        raise HTTPException(status_code=400, detail=_EMPTY)

    # Unknown, revoked, view-only, and "the memoir has been published" are one
    # indistinguishable 404. A contributor cannot act on the difference, and
    # spelling it out would tell someone holding a dead link why it died.
    if result is None:
        raise HTTPException(status_code=404, detail="link not found")

    return result


@router.get("/j/{token}/memories", response_model=list[Memory])
def get_contributions(
    token: str,
    x_participant_token: str = Header(
        ..., description="token returned by POST /j/{token}/memories"
    ),
):
    """What this one contributor has added, so they can see it landed.

    Scoped to their participant row and nothing else. A contributor must never
    see the archive or anyone else's memories, and the WHERE clause behind this
    is where that promise is kept rather than merely stated.
    """
    memories = list_contributions(token, x_participant_token)
    if memories is None:
        raise HTTPException(status_code=404, detail="link not found")

    # A contributor watches their own recording being transcribed too, so this
    # side gets the same treatment. Still scoped to their participant row.
    return refresh_pending(memories)
