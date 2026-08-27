# API layer = FastAPI routes. Development helpers, never registered in
# production.
#
# main.py only includes this router when ENABLE_DEV_ROUTES is true. That is a
# deliberately different pattern from an `if settings.debug:` check inside the
# handler: a route that is never registered cannot be called, cannot appear in
# /docs, and cannot be reached by someone who guesses the path. There is no
# runtime branch to get wrong.

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

from src.integrations.supabase_auth import TokenError, password_signin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dev", tags=["dev"])


class DevSignin(BaseModel):
    """Body of POST /dev/signin."""

    email: EmailStr
    password: str


@router.post("/signin")
def post_dev_signin(body: DevSignin):
    """Create or sign in a test user and return a real access token.

    Exists because there is a chicken-and-egg problem while building: every
    endpoint below /memoirs needs a Supabase JWT, and the only thing that
    normally mints one is the frontend login screen, which is not wired up
    yet. This gives you a token you can paste into curl or the Authorize
    button on /docs.

    The token it returns is completely ordinary - signed by Supabase, verified
    by the same code path as any other. Nothing here weakens verification; it
    just automates the signup that would otherwise happen in a browser.

    In production the browser calls Supabase directly and this API never sees
    a password. That is why this route is off by default.
    """
    try:
        session = password_signin(body.email, body.password)
    except TokenError as exc:
        # 502, not 500: the failure is upstream at Supabase, not in this app.
        # Most often "Email not confirmed", which means email confirmation is
        # still enabled in the Supabase dashboard.
        raise HTTPException(status_code=502, detail=str(exc))

    user = session.get("user") or {}
    return {
        "access_token": session.get("access_token"),
        "expires_in": session.get("expires_in"),
        "user_id": user.get("id"),
        "email": user.get("email"),
    }
