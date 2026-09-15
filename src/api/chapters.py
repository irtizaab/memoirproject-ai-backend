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

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

from src.api.dependencies import CurrentUser, current_user
from src.api.media import optional_user_id
from src.domain.chapters.assembly_service import (
    MemoirSealed,
    NothingToAssemble,
    assemble,
    generate_plan,
)
from src.domain.chapters.plan_service import (
    NoPlanYet,
    NothingLeft,
    PlanSealed,
    UnknownChapter,
)
from src.domain.chapters.plan_service import edit as edit_plan
from src.domain.chapters.plan_service import read as read_plan
from src.domain.chapters import chat_service
from src.domain.chapters.chapter_service import (
    SpanOutOfRange,
    add_comment,
    get_chapter,
    list_threads,
    reading_for_link,
    reading_for_owner,
)
from src.domain.chapters.export_service import NothingToExport, export_pdf
from src.domain.chapters.page_service import (
    BadAnchor,
    EmptyPassage,
    PageEmptied,
    UnknownBlock,
    edit_chapter,
)
from src.domain.chapters.reader_gate import (
    ReaderNameRequired,
    open_for_reading,
    reader_participant,
)
from src.domain.chapters.search_service import search_memoir
from src.models.chapter_models import (
    AssemblyResult,
    Chapter,
    ChapterEdit,
    ChatMessage,
    ChatReply,
    ChatSend,
    MemoirPlan,
    PlanUpdate,
    CommentCreate,
    CommentReceipt,
    CommentThread,
    MemoirReading,
    ReaderOpen,
    ReaderSession,
    SearchResults,
)
from src.domain.memoirs.access import owned_memoir, readable_memoir
from src.integrations.db import db

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
# Deciding what the book is
# ---------------------------------------------------------------------------
#
# Two steps, deliberately. `POST /plan` spends the model call and stores what
# came back; `POST /assemble` writes it into chapters. Between them the owner
# reads the plan and can change it, which is the whole reason the plan is a
# resource rather than a local variable.
#
# Both are bearer-only with no link path. A share token is not permission to
# decide what somebody's memoir says.


@router.post("/memoirs/{memoir_id}/plan", response_model=MemoirPlan)
async def post_plan(memoir_id: UUID, user: CurrentUser = Depends(current_user)):
    """Read the archive and decide what the book is. Owner only.

    The slow one. It reads every memory in the memoir, sends them to the
    model, verifies every quote that comes back, and stores the result — up to
    five minutes for a large archive, and the frontend allows for it.

    200 rather than 201: there is one plan per memoir, at an address the caller
    already had, and running this again replaces it rather than creating
    something new.
    """
    try:
        plan = await generate_plan(str(memoir_id), user.id)
    except NothingToAssemble:
        # The memoir is real and it is theirs, it is simply empty. 400 with
        # something the frontend can show, rather than the 404 below — telling
        # someone their memoir does not exist because they have not added a
        # memory yet would be a lie.
        raise HTTPException(
            status_code=400,
            detail="there are no memories to plan yet",
        )
    except MemoirSealed:
        raise HTTPException(
            status_code=409,
            detail="a published memoir cannot be replanned",
        )

    if plan is None:
        raise HTTPException(status_code=404, detail="memoir not found")

    # And the guide says what was built, so the outline lives in the
    # conversation that changes it rather than in a panel beside it.
    await chat_service.announce(str(memoir_id), user.id, plan)
    return plan


@router.get("/memoirs/{memoir_id}/plan", response_model=MemoirPlan)
async def get_plan(memoir_id: UUID, user: CurrentUser = Depends(current_user)):
    """The plan as it stands. Owner only.

    404 for a memoir that is not theirs and 404 for a memoir nobody has
    planned are deliberately different: the second says so in the detail, so
    the frontend can offer the button that fixes it instead of an error.
    """
    try:
        plan = await read_plan(str(memoir_id), user.id)
    except NoPlanYet:
        raise HTTPException(
            status_code=404,
            detail="this memoir has not been planned yet",
        )

    if plan is None:
        raise HTTPException(status_code=404, detail="memoir not found")

    return plan


@router.patch("/memoirs/{memoir_id}/plan", response_model=MemoirPlan)
async def patch_plan(
    memoir_id: UUID,
    update: PlanUpdate,
    user: CurrentUser = Depends(current_user),
):
    """Correct the plan before the book is written. Owner only.

    Rename a chapter, move one, drop one, reorder or reword a passage, move a
    photograph or leave it out. The whole chapter list arrives, in reading
    order, and the server renumbers from array position rather than trusting an
    ordinal a client chose.

    **An assembled plan is still editable.** It used to be frozen the moment
    the book was written from it, on the grounds that the offsets underneath
    were now load-bearing. They are — but what makes them permanent is
    publication, not assembly, and an unsealed book is rewritten wholesale by
    the next `POST /assemble`. So the outline stays a draft until the memoir is
    sealed, and assembling again is how a changed outline reaches the page.
    That does overwrite anything corrected by hand with `PATCH /chapters/{id}`,
    which is the interface's job to say and not this route's to forbid.

    What it refuses, and why each is a 409 rather than a 400: the request is
    well-formed every time and conflicts with state that already exists.

      the memoir is published  nothing about a sealed memoir changes
      the edit emptied it      a plan with no chapters is not a plan

    A chapter id the plan does not hold is a 400: that is a malformed request
    rather than a conflict, and it usually means the screen is looking at a
    plan that has since been regenerated.
    """
    try:
        plan = await edit_plan(str(memoir_id), user.id, update.model_dump()["chapters"])
    except NoPlanYet:
        raise HTTPException(
            status_code=404,
            detail="this memoir has not been planned yet",
        )
    except PlanSealed:
        raise HTTPException(
            status_code=409,
            detail="a published memoir cannot be replanned",
        )
    except NothingLeft:
        raise HTTPException(
            status_code=409,
            detail="a plan needs at least one chapter with something in it",
        )
    except UnknownChapter as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if plan is None:
        raise HTTPException(status_code=404, detail="memoir not found")

    return plan


# ---------------------------------------------------------------------------
# Making the book in the first place
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Talking to the guide
# ---------------------------------------------------------------------------
#
# Owner-only, bearer-only, like the plan. The guide answers in plain words and
# may plan the memoir again on the owner's behalf — which is why POST can take
# as long as POST /plan, and why it answers 409 once the memoir is sealed.


@router.get("/memoirs/{memoir_id}/chat", response_model=list[ChatMessage])
async def get_chat(memoir_id: UUID, user: CurrentUser = Depends(current_user)):
    """The conversation so far, oldest first. Owner only."""
    messages = await chat_service.list_messages(str(memoir_id), user.id)
    if messages is None:
        raise HTTPException(status_code=404, detail="memoir not found")
    return messages


@router.post("/memoirs/{memoir_id}/chat", response_model=ChatReply)
async def post_chat(
    memoir_id: UUID,
    message: ChatSend,
    user: CurrentUser = Depends(current_user),
):
    """Say something to the guide. Owner only.

    Slow when the guide decides to plan again: it runs the builder in this
    request, so the frontend allows the plan's timeout. A guide that cannot
    answer is still a 200 with a message saying so — nothing is broken, an
    upstream service was away, and the owner's question is kept.
    """
    try:
        result = await chat_service.send(str(memoir_id), user.id, message.body)
    except MemoirSealed:
        raise HTTPException(
            status_code=409,
            detail="a published memoir cannot be replanned",
        )
    if result is None:
        raise HTTPException(status_code=404, detail="memoir not found")
    return result


@router.post("/memoirs/{memoir_id}/assemble", response_model=AssemblyResult)
async def post_assemble(
    memoir_id: UUID, user: CurrentUser = Depends(current_user)
):
    """Write the stored plan into the memoir's chapters. Owner only.

    Bearer only, with no link path at all — this is the one route in the file
    that writes the book rather than reading it, and a share token is not
    permission to rewrite somebody's memoir.

    Fast now, and no model call: `POST /plan` did that part and this reads the
    row. 200 rather than 201: running it again replaces what was there, so it
    is not creating a resource at an address the caller did not already have.
    """
    try:
        result = await assemble(str(memoir_id), user.id)
    except NoPlanYet:
        # Not an error about the memoir — an order-of-operations answer. The
        # frontend shows the plan button, which is the thing to do next.
        raise HTTPException(
            status_code=409,
            detail="plan the memoir before assembling it",
        )
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
# Finding something in it
# ---------------------------------------------------------------------------
#
# Two routes over one query, because the owner and the family search the same
# memoir and get the same results. Anything else would have the family
# wondering what was being kept from them — and would be two result shapes to
# keep in step forever.


@router.get("/memoirs/{memoir_id}/search", response_model=SearchResults)
async def get_owner_search(
    memoir_id: UUID,
    q: str = Query(default="", max_length=200),
    user: CurrentUser = Depends(current_user),
):
    """Search the memoir, as its owner."""
    async with db() as conn, conn.cursor() as cur:
        if await owned_memoir(cur, str(memoir_id), user.id) is None:
            raise HTTPException(status_code=404, detail="memoir not found")

    return await search_memoir(str(memoir_id), q)


@router.get("/r/{token}/search", response_model=SearchResults)
async def get_reader_search(
    token: str,
    q: str = Query(default="", max_length=200),
    x_reader_token: str | None = READER_TOKEN,
):
    """The same search, for a reader who has been through the door.

    Addressed by the link like everything else on the reader's side, and
    needing the session for the same reason `GET /r/{token}` does: a forwarded
    link must not become a way to search a family's memoir.
    """
    async with db() as conn, conn.cursor() as cur:
        memoir = await readable_memoir(cur, token)
        if memoir is None:
            raise HTTPException(status_code=404, detail="link not found")
        if await reader_participant(cur, memoir, token, x_reader_token) is None:
            raise HTTPException(status_code=404, detail="link not found")
        memoir_id = str(memoir["id"])

    return await search_memoir(memoir_id, q)


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


@router.patch("/chapters/{chapter_id}", response_model=Chapter)
async def patch_chapter(
    chapter_id: UUID,
    edit: ChapterEdit,
    user: CurrentUser = Depends(current_user),
):
    """Correct one page of the book by hand. Owner only, and only before sealing.

    Bearer token and no link path, unlike the `GET` above. A share token is
    permission to read somebody's memoir, never to change what it says.

    Rename the chapter, reword a passage, reorder the page, move a photograph
    to another paragraph or another placement, or leave any of it out. What the
    owner cannot do here is write a new passage — prose with no `block_source`
    behind it is a fabricated one — or move a passage into a different chapter,
    which is the plan's job.

    **Every edited passage is re-surveyed.** A source's span is character
    offsets into the block's text, so changing the words moves them; each span
    is re-found by the substring it covered, and falls back to crediting the
    whole paragraph when the words are gone or now appear twice. The credit
    stays true and only its precision is lost, which is the trade
    `planner.verify` already makes for the same reason.

    The codes, and why each one:

      404  not this owner's chapter, or no such chapter. Never 403 — this API
           does not confirm that a stranger's memoir exists
      409  the memoir is published. Immutable is immutable; the whole comment
           layer is anchored into these characters
      409  the edit removed every passage, which would take the chapter's
           photographs with it through the anchor cascade
      400  a passage id this chapter does not hold, a blank passage, or a
           photograph pointed at something that is not a surviving paragraph
    """
    try:
        edited = await edit_chapter(
            str(chapter_id),
            user.id,
            title=edit.title,
            blocks=(
                None
                if edit.blocks is None
                else [block.model_dump() for block in edit.blocks]
            ),
        )
    except MemoirSealed:
        raise HTTPException(
            status_code=409,
            detail="a published memoir cannot be edited",
        )
    except PageEmptied:
        raise HTTPException(
            status_code=409,
            detail="a chapter needs at least one passage",
        )
    except (UnknownBlock, EmptyPassage, BadAnchor) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not edited:
        raise HTTPException(status_code=404, detail="chapter not found")

    # Read it back rather than assembling a response here: the page the owner
    # is now looking at includes freshly signed photograph URLs and the credit
    # lines joined from the archive, and `get_chapter` is the one place that
    # knows how to produce all of it.
    return await get_chapter(str(chapter_id), user_id=user.id)


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
