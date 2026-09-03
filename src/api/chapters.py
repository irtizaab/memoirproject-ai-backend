# API layer = FastAPI routes. The finished memoir, and the talking about it.
#
# Every route here accepts either credential, the same way `media.py` does: an
# owner's bearer token, or the `X-Link-Token` of a live **view** link. The
# handler passes on whatever arrived and the domain layer decides whether it is
# enough. A second parallel set of public endpoints was the alternative, and
# that is how two code paths that were meant to return the same thing stop.
#
# `GET /r/{token}` is the exception with no bearer path at all — the read-side
# twin of `GET /j/{token}`, and the entry point the reader is opened by.

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException

from src.api.dependencies import CurrentUser, current_user
from src.api.media import optional_user_id
from src.domain.chapters.chapter_service import (
    NameRequired,
    SpanOutOfRange,
    add_comment,
    get_chapter,
    list_threads,
    reading_for_link,
    reading_for_owner,
)
from src.models.chapter_models import (
    Chapter,
    CommentCreate,
    CommentReceipt,
    CommentThread,
    MemoirReading,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chapters"])

# `optional_user_id` is imported from `media.py` rather than written again: it
# is the same question ("is there a usable bearer token on this request?") and
# the same answer — None rather than a 401, because arriving without one is a
# legitimate way to call these.


def _either(user_id: str | None, link_token: str | None) -> None:
    """401 when the caller offered no credential at all.

    Distinct from the 404 that follows a credential which simply does not reach
    this chapter. "You did not tell me who you are" and "that is not yours" are
    different answers and the frontend does different things with them.
    """
    if user_id is None and link_token is None:
        raise HTTPException(
            status_code=401,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# The covers
# ---------------------------------------------------------------------------


@router.get("/r/{token}", response_model=MemoirReading)
def get_reading(token: str):
    """Resolve a view link into the book it opens.

    `/r/` for read, beside `/j/` for join. Short for the same reason: this URL
    is forwarded in WhatsApp messages and read aloud over the phone.

    No `Depends(current_user)`, and that is the product working as designed —
    the family reading a finished memoir have no accounts and never will. The
    token is 24 random bytes and is checked against `revoked_at IS NULL`, so a
    link that escapes into the wrong hands can be killed.

    `response_model` is load-bearing here. It filters the outgoing dict down to
    what `MemoirReading` declares, which is what keeps the owner's private
    `never_forget` answer away from anyone the link was forwarded to — the same
    reasoning as `LinkInvitation` on `GET /j/{token}`.
    """
    reading = reading_for_link(token)

    # Unknown, revoked and wrong-scope look identical from out here, on purpose.
    if reading is None:
        raise HTTPException(status_code=404, detail="link not found")

    return reading


@router.get("/memoirs/{memoir_id}/chapters", response_model=MemoirReading)
def get_owner_reading(memoir_id: UUID, user: CurrentUser = Depends(current_user)):
    """The same covers, for the owner reading their own memoir.

    Exists so an owner can read before publishing, and without a view link
    having been issued. 404 rather than 403 for a memoir that is not theirs —
    this API never confirms a stranger's memoir exists.
    """
    reading = reading_for_owner(str(memoir_id), user.id)
    if reading is None:
        raise HTTPException(status_code=404, detail="memoir not found")
    return reading


# ---------------------------------------------------------------------------
# One chapter
# ---------------------------------------------------------------------------


@router.get("/chapters/{chapter_id}", response_model=Chapter)
def get_one_chapter(
    chapter_id: UUID,
    user_id: str | None = Depends(optional_user_id),
    x_link_token: str | None = Header(
        default=None, description="view link token, for a reader with no account"
    ),
):
    """One chapter: its prose, its photographs, its sources, its conversation.

    One response rather than four, because a chapter is one page and four round
    trips to draw it is four chances to show half of one.
    """
    _either(user_id, x_link_token)

    chapter = get_chapter(
        str(chapter_id), user_id=user_id, link_token=x_link_token
    )
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    return chapter


# ---------------------------------------------------------------------------
# The layer that stays open
# ---------------------------------------------------------------------------


@router.get("/chapters/{chapter_id}/comments", response_model=list[CommentThread])
def get_comments(
    chapter_id: UUID,
    user_id: str | None = Depends(optional_user_id),
    x_link_token: str | None = Header(
        default=None, description="view link token, for a reader with no account"
    ),
):
    """Just the conversation.

    `GET /chapters/{id}` already carries it. This exists for the one thing that
    changes after the page is drawn — somebody else commenting — so the reader
    can pick that up without re-fetching the prose and re-signing every
    photograph in the chapter.
    """
    _either(user_id, x_link_token)

    threads = list_threads(
        str(chapter_id), user_id=user_id, link_token=x_link_token
    )
    if threads is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    return threads


@router.post(
    "/chapters/{chapter_id}/comments", response_model=CommentReceipt, status_code=201
)
def post_comment(
    chapter_id: UUID,
    body: CommentCreate,
    user_id: str | None = Depends(optional_user_id),
    x_link_token: str | None = Header(
        default=None, description="view link token, for a reader with no account"
    ),
):
    """Say something about a passage, or reply to somebody who did.

    The only write in this API that a **published** memoir accepts. Everything
    else answers 409 once `status` flips; this is the layer the product
    promises stays open forever, and refusing it here would break the sentence
    the confirm screen makes people tick a box to agree to.

    Three failures worth telling apart:

      401  no credential at all
      400  a reader who did not say who they are, or an offset past the end of
           the paragraph — both the caller's mistake, both fixable
      404  a chapter, block or thread this credential cannot reach, or that
           does not exist. Deliberately undistinguished.
    """
    _either(user_id, x_link_token)

    try:
        receipt = add_comment(
            str(chapter_id),
            body.model_dump(),
            user_id=user_id,
            link_token=x_link_token,
        )
    except NameRequired:
        raise HTTPException(
            status_code=400, detail="say who this is from before leaving it"
        )
    except SpanOutOfRange:
        raise HTTPException(
            status_code=400, detail="that comment points past the end of the passage"
        )

    if receipt is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    return receipt
