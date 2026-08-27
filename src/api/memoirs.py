# API layer = FastAPI routes. Keep this thin: read the request, call domain/,
# turn the result into a status code.
#
# Note what this file does with the two failure cases from claim_draft(). The
# service returns None for one and raises DraftIncomplete for the other, and
# neither of those is an HTTP concept. Deciding that one is a 404 and the other
# a 400 happens here, and only here.

import logging

from fastapi import APIRouter, Depends, Header, HTTPException

from src.api.dependencies import CurrentUser, current_user
from src.domain.memoirs.memoir_service import DraftIncomplete, claim_draft
from src.models.memoir_models import ClaimRequest, MemoirSummary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memoirs", tags=["memoirs"])


@router.post("/claim", response_model=MemoirSummary, status_code=201)
def post_claim(
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
        memoir = claim_draft(
            draft_id=str(body.draft_id),
            draft_token=x_draft_token,
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
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
