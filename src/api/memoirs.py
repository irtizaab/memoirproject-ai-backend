# API layer = FastAPI routes. Keep this thin: read the request, call domain/,
# turn the result into a status code.
#
# Note what this file does with the two failure cases from claim_draft(). The
# service returns None for one and raises DraftIncomplete for the other, and
# neither of those is an HTTP concept. Deciding that one is a 404 and the other
# a 400 happens here, and only here.

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException

from src.api.dependencies import CurrentUser, current_user
from src.domain.memoirs.memoir_service import (
    AlreadyHasMemoir,
    AlreadyPublished,
    DraftIncomplete,
    NothingToPublish,
    claim_draft,
    publish_memoir,
    replace_passphrase,
)
from src.domain.memoirs.passphrase import MINIMUM_LENGTH, PassphraseTooShort
from src.models.memoir_models import (
    ClaimRequest,
    MemoirPublication,
    MemoirSummary,
    PassphraseRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memoirs", tags=["memoirs"])


@router.post("/claim", response_model=MemoirSummary, status_code=201)
async def post_claim(
    body: ClaimRequest,
    x_draft_token: str = Header(..., description="token from POST /drafts"),
    user: CurrentUser = Depends(current_user),
):
    """Turn a finished draft into a real memoir owned by the caller.

    The single most important request in the onboarding flow: it is the moment
    an anonymous set of answers becomes a family's memoir.

    Note that this route needs BOTH credentials, and they prove different
    things:

      - `Authorization: Bearer <supabase jwt>` proves who you are. It is what
        decides which account ends up owning the memoir.
      - `X-Draft-Token` proves you are the browser that started this draft.
        Without it, any logged-in user who learned a draft id could claim
        someone else's answers.

    Neither substitutes for the other, because the person and the browser
    session became linked only a moment ago, at signup.

    201, not 200 - a resource was created and this is the only time it ever
    will be for this draft.
    """
    try:
        memoir = await claim_draft(
            draft_id=str(body.draft_id),
            draft_token=x_draft_token,
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
        )
    except AlreadyHasMemoir:
        # An account owns one memoir. Not an error in the sense of something
        # having gone wrong — they already have what they were trying to make,
        # and the useful thing is to say so and point at it.
        #
        # 409 rather than 400: the request was well-formed, it conflicts with
        # state that already exists. The draft is left unclaimed, so nothing was
        # consumed by the attempt.
        raise HTTPException(
            status_code=409,
            detail="this account already has a memoir",
        )
    except DraftIncomplete:
        # The draft is real and it is yours, but it is not finished. A 400 with
        # a message the frontend can show, rather than the 404 below - sending
        # someone back to a "not found" screen when their answers are safely
        # stored would be a lie.
        raise HTTPException(
            status_code=400,
            detail="draft is missing the subject's name",
        )

    # Same None-means-404 rule as the drafts routes: no such draft, wrong
    # token, and already claimed are one indistinguishable response.
    if memoir is None:
        raise HTTPException(status_code=404, detail="draft not found")

    return memoir


@router.post("/{memoir_id}/publish", response_model=MemoirPublication)
async def post_publish(
    memoir_id: UUID,
    body: PassphraseRequest,
    user: CurrentUser = Depends(current_user),
):
    """Seal the memoir and protect it with a passphrase. Owner only.

    The most irreversible request in the product. Afterwards the text can never
    change — every comment and every source in it is anchored to character
    offsets that are only safe because of that — so the route refuses a memoir
    with no chapters and refuses to run twice.

    The response carries the view token and not the passphrase. The owner
    chose it, and telling it back to them here would put it in a response body
    and every log that records one.
    """
    try:
        published = await publish_memoir(str(memoir_id), user.id, body.passphrase)
    except PassphraseTooShort:
        # Pydantic already enforces this on the way in; reaching here means the
        # value was whitespace padded out to eight. The rule, not the value.
        raise HTTPException(
            status_code=400,
            detail=f"the passphrase must be at least {MINIMUM_LENGTH} characters",
        )
    except NothingToPublish:
        # 400 and a sentence: the memoir is real and it is theirs, it just has
        # no chapters yet. Sending them to a "not found" screen would be a lie.
        raise HTTPException(
            status_code=400,
            detail="assemble the memoir before publishing it",
        )
    except AlreadyPublished:
        # 409 — well-formed, and conflicting with state that already exists.
        # Forgetting the passphrase is not a reason to publish again; that is
        # what PUT /passphrase is for.
        raise HTTPException(status_code=409, detail="this memoir is already published")

    if published is None:
        raise HTTPException(status_code=404, detail="memoir not found")

    return published


@router.put("/{memoir_id}/passphrase", status_code=204)
async def put_passphrase(
    memoir_id: UUID,
    body: PassphraseRequest,
    user: CurrentUser = Depends(current_user),
):
    """Replace the passphrase. Owner only.

    There is no route that reads the old one back, because nothing in the
    building can: it is a scrypt hash. Replacing is the only move available,
    and it locks out everyone who was told the previous one — which is what
    somebody asking for this wants, since the usual reason to ask is that it
    reached someone it should not have.

    204: nothing to return that the caller does not already know.
    """
    try:
        replaced = await replace_passphrase(str(memoir_id), user.id, body.passphrase)
    except PassphraseTooShort:
        raise HTTPException(
            status_code=400,
            detail=f"the passphrase must be at least {MINIMUM_LENGTH} characters",
        )

    if replaced is None:
        raise HTTPException(status_code=404, detail="memoir not found")
