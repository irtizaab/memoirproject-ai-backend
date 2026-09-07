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
#
# Every link-addressed route also wants `X-Reader-Token`: the session issued by
# `POST /r/{token}/open` in exchange for the passphrase and a name. The link
# says which memoir; the session says who is holding it. A link that has been
# forwarded to somebody who was never told the passphrase opens nothing, and
# there is no way to read a family's memoir without saying who you are.

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from src.api.dependencies import CurrentUser, current_user
from src.api.media import optional_user_id
from src.domain.chapters.assembly_service import (
    MemoirSealed,
    NothingToAssemble,
    assemble,
)
from src.domain.chapters.chapter_service import (
    SpanOutOfRange,
    add_comment,
    get_chapter,
    list_threads,
    reading_for_link,
    reading_for_owner,
)
from src.domain.chapters.export_service import NothingToExport, export_pdf
from src.domain.chapters.reader_gate import ReaderNameRequired, open_for_reading
from src.models.chapter_models import (
    AssemblyResult,
    Chapter,
    CommentCreate,
    CommentReceipt,
    CommentThread,
    MemoirReading,
    ReaderOpen,
    ReaderSession,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chapters"])

# `optional_user_id` is imported from `media.py` rather than written again: it
# is the same question ("is there a usable bearer token on this request?") and
# the same answer — None rather than a 401, because arriving without one is a
# legitimate way to call these.


READER_TOKEN = Header(
    default=None,
    alias="X-Reader-Token",
    description="session from POST /r/{token}/open",
)

LINK_TOKEN = Header(
    default=None,
    alias="X-Link-Token",
    description="view link token, for a reader with no account",
)


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


@router.post("/r/{token}/open", response_model=ReaderSession)
async def post_open(
    token: str,
    body: ReaderOpen,
    user_id: str | None = Depends(optional_user_id),
):
    """The door. Exchange a passphrase and a name for a reader session.

    Every way of failing answers 404: an unknown token, a revoked one, a
    contribute link, a memoir nobody has protected yet, and the wrong
    passphrase. Telling a real link with a bad passphrase apart from a link
    that was never real is what turns a leaked link into a target worth
    guessing at.

    Not rate limited here, and it should be at the edge. scrypt makes each
    attempt cost the server about as much as it costs the attacker, which
    bounds the damage but does not remove it.

    The owner is recognised by their bearer token and let straight through
    without a passphrase or a name: they set the one and their account carries
    the other.
    """
    try:
        session = await open_for_reading(
            token,
            passphrase=body.passphrase,
            display_name=body.display_name,
            relationship=body.relationship,
            participant_token=body.participant_token,
            user_id=user_id,
        )
    except ReaderNameRequired:
        # 400 and not 404: the link and the passphrase were right, and what is
        # missing is something the person can fix in the form in front of them.
        raise HTTPException(
            status_code=400, detail="say who you are before opening the memoir"
        )

    if session is None:
        raise HTTPException(status_code=404, detail="link not found")

    return session


@router.get("/r/{token}", response_model=MemoirReading)
async def get_reading(token: str, x_reader_token: str | None = READER_TOKEN):
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
    reading = await reading_for_link(token, x_reader_token)

    # Unknown, revoked, wrong-scope and no-session look identical from out
    # here, on purpose.
    if reading is None:
        raise HTTPException(status_code=404, detail="link not found")

    return reading


@router.get("/memoirs/{memoir_id}/chapters", response_model=MemoirReading)
async def get_owner_reading(memoir_id: UUID, user: CurrentUser = Depends(current_user)):
    """The same covers, for the owner reading their own memoir.

    Exists so an owner can read before publishing, and without a view link
    having been issued. 404 rather than 403 for a memoir that is not theirs —
    this API never confirms a stranger's memoir exists.
    """
    reading = await reading_for_owner(str(memoir_id), user.id)
    if reading is None:
        raise HTTPException(status_code=404, detail="memoir not found")
    return reading


# ---------------------------------------------------------------------------
# Making the book in the first place
# ---------------------------------------------------------------------------


@router.post("/memoirs/{memoir_id}/assemble", response_model=AssemblyResult)
async def post_assemble(
    memoir_id: UUID, user: CurrentUser = Depends(current_user)
):
    """Turn everything in the archive into chapters. Owner only.

    Bearer only, with no link path at all — this is the one route in the file
    that writes the book rather than reading it, and a share token is not
    permission to rewrite somebody's memoir.

    200 rather than 201: running it again replaces what was there, so it is not
    creating a resource at an address the caller did not already have.
    """
    try:
        result = await assemble(str(memoir_id), user.id)
    except NothingToAssemble:
        # The memoir is real and it is theirs, it is simply empty. 400 with
        # something the frontend can show, rather than the 404 below — telling
        # someone their memoir does not exist because they have not added a
        # memory yet would be a lie.
        raise HTTPException(
            status_code=400,
            detail="there are no memories to assemble yet",
        )
    except MemoirSealed:
        # 409: the request was well-formed and conflicts with state that
        # already exists. A published memoir cannot be reassembled — every
        # comment in it is anchored to characters in the text it would rewrite.
        raise HTTPException(
            status_code=409,
            detail="a published memoir cannot be reassembled",
        )

    if result is None:
        raise HTTPException(status_code=404, detail="memoir not found")

    return result


@router.get(
    "/memoirs/{memoir_id}/export.pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def get_export(memoir_id: UUID, user: CurrentUser = Depends(current_user)):
    """The memoir as a file the family can keep. Owner only.

    Bearer only and no link path, like assembly: a view link lets somebody read
    the book, and handing every reader a print-ready copy of a private family
    memoir is a different decision that nobody has made.

    `Response` rather than `StreamingResponse` because the whole document is
    already bytes in memory by the time this returns — streaming it would mean
    holding a database connection open while a phone downloads.

    `Content-Disposition: attachment` so a browser saves it under the subject's
    name instead of rendering it in a tab called `export.pdf`.
    """
    try:
        exported = await export_pdf(str(memoir_id), user.id)
    except NothingToExport:
        raise HTTPException(
            status_code=400,
            detail="assemble the memoir before exporting it",
        )

    if exported is None:
        raise HTTPException(status_code=404, detail="memoir not found")

    content, filename = exported
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# One chapter
# ---------------------------------------------------------------------------


@router.get("/chapters/{chapter_id}", response_model=Chapter)
async def get_one_chapter(
    chapter_id: UUID,
    user_id: str | None = Depends(optional_user_id),
    x_link_token: str | None = LINK_TOKEN,
    x_reader_token: str | None = READER_TOKEN,
):
    """One chapter: its prose, its photographs, its sources, its conversation.

    One response rather than four, because a chapter is one page and four round
    trips to draw it is four chances to show half of one.
    """
    _either(user_id, x_link_token)

    chapter = await get_chapter(
        str(chapter_id),
        user_id=user_id,
        link_token=x_link_token,
        reader_token=x_reader_token,
    )
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    return chapter


# ---------------------------------------------------------------------------
# The layer that stays open
# ---------------------------------------------------------------------------


@router.get("/chapters/{chapter_id}/comments", response_model=list[CommentThread])
async def get_comments(
    chapter_id: UUID,
    user_id: str | None = Depends(optional_user_id),
    x_link_token: str | None = LINK_TOKEN,
    x_reader_token: str | None = READER_TOKEN,
):
    """Just the conversation.

    `GET /chapters/{id}` already carries it. This exists for the one thing that
    changes after the page is drawn — somebody else commenting — so the reader
    can pick that up without re-fetching the prose and re-signing every
    photograph in the chapter.
    """
    _either(user_id, x_link_token)

    threads = await list_threads(
        str(chapter_id),
        user_id=user_id,
        link_token=x_link_token,
        reader_token=x_reader_token,
    )
    if threads is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    return threads


@router.post(
    "/chapters/{chapter_id}/comments", response_model=CommentReceipt, status_code=201
)
async def post_comment(
    chapter_id: UUID,
    body: CommentCreate,
    user_id: str | None = Depends(optional_user_id),
    x_link_token: str | None = LINK_TOKEN,
    x_reader_token: str | None = READER_TOKEN,
):
    """Say something about a passage, or reply to somebody who did.

    The only write in this API that a **published** memoir accepts. Everything
    else answers 409 once `status` flips; this is the layer the product
    promises stays open forever, and refusing it here would break the sentence
    the confirm screen makes people tick a box to agree to.

    Three failures worth telling apart:

      401  no credential at all
      400  an offset past the end of the paragraph — the caller's mistake, and
           fixable
      404  a chapter, block or thread this credential cannot reach, or that
           does not exist. Deliberately undistinguished.

    Nobody is asked who they are here. That was settled at the door, and the
    reader session on this request carries it.
    """
    _either(user_id, x_link_token)

    try:
        receipt = await add_comment(
            str(chapter_id),
            body.model_dump(),
            user_id=user_id,
            link_token=x_link_token,
            reader_token=x_reader_token,
        )
    except SpanOutOfRange:
        raise HTTPException(
            status_code=400, detail="that comment points past the end of the passage"
        )

    if receipt is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    return receipt
