# API layer = FastAPI routes. The owner's view of who is in their memoir.
#
# Everything here is owner-only. A contributor has no business seeing the guest
# list — they were invited to add something, not to find out who else was
# asked.

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import CurrentUser, current_user
from src.domain.contributors.contributor_service import (
    CannotMerge,
    list_contributors,
    merge_participants,
    reissue_link,
)
from src.models.account_models import (
    ContributorsOverview,
    MergeResult,
    ShareLink,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memoirs", tags=["contributors"])


@router.get("/{memoir_id}/contributors", response_model=ContributorsOverview)
def get_contributors(memoir_id: UUID, user: CurrentUser = Depends(current_user)):
    """Everyone in the memoir, plus the live share link.

    One response rather than two endpoints, because the screen shows them
    together and splitting it would only make the page assemble itself in
    front of the reader.
    """
    overview = list_contributors(str(memoir_id), user.id)
    if overview is None:
        raise HTTPException(status_code=404, detail="memoir not found")
    return overview


@router.post(
    "/{memoir_id}/contributors/{loser_id}/merge-into/{winner_id}",
    response_model=MergeResult,
)
def post_merge_contributors(
    memoir_id: UUID,
    loser_id: UUID,
    winner_id: UUID,
    user: CurrentUser = Depends(current_user),
):
    """Record that two entries in the contributors list are one person.

    The same human on a second device becomes a second participant, because
    identity is a token held in a browser and a phone and a laptop hold two.
    This is how the owner says so.

    Only the owner can do it, and only they should: two people genuinely share
    a name, and no rule this API could apply would tell two cousins called Ali
    apart. Nothing here matches on names — both ids are named by the caller.

    400 for a request that does not make sense (merging somebody into
    themselves, or involving the owner). 404 for a participant that is not in
    this memoir, which stays indistinguishable from one that does not exist.

    Not reversible. The frontend says so before calling it.
    """
    try:
        result = merge_participants(
            str(memoir_id), user.id, str(loser_id), str(winner_id)
        )
    except CannotMerge as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if result is None:
        raise HTTPException(status_code=404, detail="memoir not found")
    return result


@router.post("/{memoir_id}/link/reissue", response_model=ShareLink, status_code=201)
def post_reissue_link(memoir_id: UUID, user: CurrentUser = Depends(current_user)):
    """Kill the current share link and issue a new one.

    Destructive, and the frontend must say so before calling it: everybody
    holding the old URL — including people who were going to contribute this
    weekend — loses access the moment this returns. It is the remedy for a link
    that has travelled further than intended, and there is no undo.

    Nothing already contributed is affected. Revoking a link closes the door;
    it does not empty the room.
    """
    link = reissue_link(str(memoir_id), user.id)
    if link is None:
        raise HTTPException(status_code=404, detail="memoir not found")
    return link
