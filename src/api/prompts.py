# API layer = FastAPI routes. Read the request, call domain/, pick a status.
#
# Two audiences, and the split is the same one `memories.py` makes:
#
#   /memoirs/{id}/questions   the owner. Bearer token. Writes the library.
#   /j/{token}/questions      a contributor. No account, ever. Reads the four
#                             or five questions written for people like them.
#
# They live together because they are the same resource seen from two sides,
# and keeping them adjacent makes it obvious when one grows a field the other
# should not be able to read. `ContributorQuestions` is where that line is
# actually held — see `models/prompt_models.py`.

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from src.api.dependencies import CurrentUser, current_user
from src.domain.prompts.prompt_service import (
    LinkNotUsable,
    NothingGenerated,
    delete_question,
    for_contributor,
    generate,
    get_library,
    set_mode,
    update_question,
)
from src.models.prompt_models import (
    ContributorQuestions,
    GenerateRequest,
    ModeUpdate,
    Question,
    QuestionLibrary,
    QuestionUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["questions"])

# 404 for "no such memoir" and "not yours" alike, as everywhere else. Answering
# 403 for the second would confirm the id belongs to a real memoir.
_NOT_FOUND = "memoir not found"

# Unknown, revoked, view-only, published, and "that token is nobody" are one
# indistinguishable 404 — the same sentence `memories.py` uses, for the same
# reason. A contributor cannot act on the difference.
_LINK_NOT_FOUND = "link not found"


# ---------------------------------------------------------------------------
# The owner
# ---------------------------------------------------------------------------


@router.get("/memoirs/{memoir_id}/questions", response_model=QuestionLibrary)
async def get_questions(
    memoir_id: UUID, user: CurrentUser = Depends(current_user)
):
    """The whole questions screen: the mode, the notes, the library, the set.

    One response rather than four, because the screen renders all of it
    together and four round trips to build one page is four chances to show a
    quarter of it.
    """
    library = await get_library(str(memoir_id), user.id)
    if library is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return library


@router.post("/memoirs/{memoir_id}/questions/generate", response_model=QuestionLibrary)
async def post_generate(
    memoir_id: UUID,
    body: GenerateRequest,
    user: CurrentUser = Depends(current_user),
):
    """Draft a library from what the owner has said about the subject.

    Returns the library as `GET` would, so the screen re-renders from this one
    response instead of generating and then refetching.

    503 rather than 500 when the model could not produce one: nothing is broken
    here, an upstream service was unavailable or switched off, and the owner
    can try again or use the standard set — which the same response body would
    have shown them anyway. The notes they typed are saved before the call, so
    a failure never costs them the paragraph.
    """
    try:
        library = await generate(
            str(memoir_id), user.id, body.notes, body.replace_edited
        )
    except NothingGenerated as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "questions could not be written just now — the standard "
                "questions are still there, and you can try again"
            ),
        ) from exc

    if library is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return library


@router.patch("/memoirs/{memoir_id}/questions/mode", response_model=QuestionLibrary)
async def patch_mode(
    memoir_id: UUID,
    body: ModeUpdate,
    user: CurrentUser = Depends(current_user),
):
    """Choose which set of questions contributors see.

    Switching to `standard` does not delete anything. The owner is choosing
    what is used, not discarding what was written, and switching back returns
    the library they had.
    """
    library = await set_mode(str(memoir_id), user.id, body.mode)
    if library is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return library


@router.patch("/questions/{prompt_id}", response_model=Question)
async def patch_question(
    prompt_id: UUID,
    body: QuestionUpdate,
    user: CurrentUser = Depends(current_user),
):
    """Rewrite one question by hand.

    Addressed by the question's own id rather than under the memoir, because
    the screen edits one row at a time and already holds the id. Ownership is
    reached by joining up to `memoir` — the same shape `PATCH /memories/{id}`
    uses.

    The response carries the updated row so the client can see `source` has
    become `owner`, which is what makes this question survive a reprompt.
    """
    question = await update_question(str(prompt_id), user.id, body.body)
    if question is None:
        raise HTTPException(status_code=404, detail="question not found")
    return question


@router.delete("/questions/{prompt_id}", status_code=204)
async def remove_question(
    prompt_id: UUID, user: CurrentUser = Depends(current_user)
):
    """Delete one question.

    204 with no body: there is nothing meaningful to return about something
    that no longer exists.
    """
    if not await delete_question(str(prompt_id), user.id):
        raise HTTPException(status_code=404, detail="question not found")


# ---------------------------------------------------------------------------
# The contributor
# ---------------------------------------------------------------------------


@router.get("/j/{token}/questions", response_model=ContributorQuestions)
async def get_contributor_questions(
    token: str,
    relationship: str | None = Query(
        None, description="how they say they knew the subject, if they have said"
    ),
    x_participant_token: str | None = Header(
        None, description="token returned by POST /j/{token}/memories"
    ),
):
    """The four or five questions written for people like this contributor.

    The link is the only credential, unlike `GET /j/{token}/memories` — which
    needs a participant token because it returns somebody's own memories. This
    returns question text and a group name, both of which the same link already
    entitles the holder to see. Requiring a token here made the library
    invisible to first-time contributors, who are the people it is for.

    `relationship` is what they have just said on the form; the participant
    token, when there is one, is what they said last time. Neither, and they
    get the `other` set.

    `response_model` is doing real work here. The domain layer reads rows that
    carry ids, a source and a mode; `ContributorQuestions` declares text and a
    group name, so none of the rest can reach somebody the link was forwarded
    to. See the header of `models/prompt_models.py`.

    Never empty in practice: a group with no custom questions falls through to
    the standard set, because the blank page is the problem this whole feature
    exists to solve.
    """
    try:
        return await for_contributor(token, x_participant_token, relationship)
    except LinkNotUsable:
        raise HTTPException(status_code=404, detail=_LINK_NOT_FOUND)
