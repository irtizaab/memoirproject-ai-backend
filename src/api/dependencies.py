# Shared pieces of the API layer that more than one router needs.
#
# This is where "who is calling?" is answered. It sits in api/ rather than
# core/ because it is HTTP-specific: it reads a request header and raises 401.
# The domain layer never imports this — it receives a plain user id.
#
# ---------------------------------------------------------------------------
# What "dependency" means here
#
# A FastAPI dependency is a function that runs before your route handler and
# hands it a ready-made value. You declare it in the signature:
#
#     def my_route(user: CurrentUser = Depends(current_user)):
#
# and FastAPI calls `current_user()` first, then passes the result in as
# `user`. If the dependency raises, the route body never runs at all.
#
# Two reasons this beats calling a check_auth() helper on line 1 of every
# handler. First, you cannot forget it — a route without the parameter is
# visibly unauthenticated, rather than accidentally so. Second, FastAPI reads
# these signatures to build /docs, so every protected endpoint shows up with a
# lock icon and an Authorize button for free.
# ---------------------------------------------------------------------------

import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.integrations.supabase_auth import TokenError, verify_access_token

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated caller, distilled from their token's claims.

    Deliberately not the raw claims dict. Routes and services should depend on
    the three fields they actually use, not on the shape of a Supabase JWT —
    that keeps the auth provider swappable and stops `claims["sub"]` being
    sprinkled through the codebase.

    `id` is the Supabase auth.users id, which is exactly what user_account.id
    references. One identity, one primary key, no mapping table.

    Frozen because a request handler has no business rewriting who the caller
    is halfway through.
    """

    id: str
    email: str
    full_name: str


# auto_error=False means: if the Authorization header is missing or malformed,
# hand back None instead of raising. We want that control, because HTTPBearer's
# built-in error is a 403, and "you did not tell me who you are" is a 401.
# 403 means "I know who you are and you still may not"; that is a different
# thing, and the frontend reacts to them differently — 401 means show the login
# screen, 403 means show a permission error.
_bearer = HTTPBearer(auto_error=False, description="Supabase access token")


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    """Resolve the caller from the `Authorization: Bearer <token>` header.

    Raises 401 if the header is absent, malformed, or the token doesn't verify.
    Every failure returns the same generic message on purpose — telling a
    caller "expired" versus "bad signature" is free reconnaissance.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=401,
            detail="not authenticated",
            # The spec-correct way to say "send me a bearer token". Some HTTP
            # clients look at this to decide how to retry.
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = verify_access_token(credentials.credentials)
    except TokenError:
        raise HTTPException(
            status_code=401,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Supabase puts anything collected at signup into user_metadata. Google
    # sign-in fills in `full_name`; an email/password signup usually leaves it
    # empty. Both are normal, so treat a missing name as "" rather than an
    # error — the claim step supplies a sensible fallback.
    metadata = claims.get("user_metadata") or {}
    full_name = metadata.get("full_name") or metadata.get("name") or ""

    return CurrentUser(
        id=claims["sub"],
        email=claims.get("email") or "",
        full_name=full_name.strip(),
    )
