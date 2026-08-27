# Pydantic models = the request/response "shape" for a feature.
#
# The response models here are not decoration. Routes attach them with
# `response_model=`, which makes FastAPI filter the outgoing dict down to
# exactly these fields. That matters most for the public invitation endpoint:
# if someone later adds a column to that SELECT, it cannot leak to
# unauthenticated contributors, because a field not declared here is dropped.

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ClaimRequest(BaseModel):
    """Body of POST /memoirs/claim.

    Only the draft's id. The secret that proves the caller owns the draft
    travels in the X-Draft-Token header, the same as the PATCH route — a
    credential belongs in a header, not mixed in with the payload.

    `draft_id` is a UUID rather than a str so a malformed id is rejected here
    with a clear 422 naming the field, instead of reaching Postgres and coming
    back as a generic "invalid_value" 400.
    """

    draft_id: UUID = Field(..., description="id returned by POST /drafts")


class MemoirSummary(BaseModel):
    """One memoir as its owner sees it.

    Returned by POST /memoirs/claim and, in a list, by GET /me.

    `link_token` is the shareable secret from memoir_link. The API returns the
    token alone, not a full URL — the frontend knows its own domain and builds
    `memoirproject.co/j/{link_token}` itself. Hardcoding a domain in the API
    would mean staging links pointing at production.
    """

    id: UUID
    subject_name: str
    born_year: int | None
    through_year: int | None
    subject_is_living: bool | None
    never_forget: str | None
    status: str
    created_at: datetime

    # Optional because a link can be revoked. memoir_link exists as its own
    # table precisely so a leaked token can be killed and reissued, which
    # leaves the memoir briefly with no live link — a real state, not an error.
    link_token: str | None


class AccountOverview(BaseModel):
    """Body of GET /me — who the caller is, and what they own.

    `id`, `email` and `full_name` come from the verified token, so this works
    even for someone who has signed up but not yet claimed a draft. In that
    case `memoirs` is simply empty; it is not a 404. "You exist but own
    nothing" is a normal state, not an error.
    """

    id: UUID
    email: str
    full_name: str
    memoirs: list[MemoirSummary]


class LinkInvitation(BaseModel):
    """Body of GET /j/{token} — what a contributor sees before contributing.

    Unauthenticated: the token in the URL is the entire credential. So this is
    the narrowest model in the file, and every field is here on purpose.

    `never_forget` is deliberately absent. It is the owner's private answer to
    "what should we never forget about them", not a caption for the invite
    screen. Anyone who forwards the link would be forwarding that too.
    """

    memoir_id: UUID
    subject_name: str
    born_year: int | None
    through_year: int | None
    subject_is_living: bool | None
    scope: str
    invited_by: str
