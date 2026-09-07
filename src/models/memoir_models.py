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

    # How many chapters the archive has been assembled into, and therefore the
    # answer to "is there a book yet". The dashboard shows nothing that opens
    # the reader until this is above zero, because a memoir with no chapters
    # opens onto an empty page.
    #
    # A count rather than an `assembled_at` timestamp: chapters existing is the
    # fact the frontend actually needs, it is true whether they were assembled
    # or inserted by SQL, and migration 0011 warned specifically against adding
    # columns for a step that had not been written yet.
    #
    # Defaulted, because a memoir being claimed has none and POST /memoirs/claim
    # returns this same model a moment after the row is created.
    chapter_count: int = 0

    # When it was sealed, or null while it is still a draft. `status` says the
    # same thing, and this says *when* — which is what the dashboard prints
    # beside the link.
    published_at: datetime | None = None

    # The token that opens the reader, distinct from `link_token` above: that
    # one collects memories, this one hands over the finished book. Null until
    # publication issues it.
    #
    # Note what is deliberately absent: `view_passphrase_hash`. It is a
    # password hash on a row this model is built from, and `response_model`
    # filtering is the only thing that would keep a `SELECT *` from putting it
    # in a browser.
    view_token: str | None = None


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


class PassphraseRequest(BaseModel):
    """Body of POST /memoirs/{id}/publish and PUT /memoirs/{id}/passphrase.

    One field, and the only password this product has. `min_length` is checked
    here so the 422 names the field, and again in `hash_passphrase` — which is
    the last thing between a memoir and a passphrase of "a", and is reachable
    from anywhere.

    No maximum worth enforcing below scrypt's own: a family who want a whole
    sentence should have one, and a long passphrase is the good case.

    It is never stored, never logged and never returned. The owner chooses it
    and tells people themselves.
    """

    passphrase: str = Field(..., min_length=8, max_length=256)


class MemoirPublication(BaseModel):
    """What sealing a memoir gives back.

    The token and the timestamp, and pointedly not the passphrase: echoing it
    would put it in a response body, a browser cache and any log that ever
    records one.

    `view_token` and not a URL, for the reason `MemoirSummary.link_token` gives
    — the API does not know which domain the frontend is served from, and a
    staging deployment handing out production links would be a real bug.
    """

    view_token: str
    published_at: datetime
