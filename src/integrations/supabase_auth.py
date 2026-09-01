# Integrations = thin wrappers around external services. Supabase Auth is an
# external service, so "how do I check whether this token is real" lives here.
#
# Nothing in this file knows what a memoir is. It answers exactly one question —
# "is this a genuine, unexpired access token from our Supabase project, and who
# does it belong to?" — and hands back the claims. Deciding what that person is
# allowed to do is the domain layer's job.

import logging

import httpx
import jwt
from jwt import PyJWKClient

from src.core.config import settings

logger = logging.getLogger(__name__)


class TokenError(Exception):
    """The token was missing, malformed, expired, or not ours.

    A deliberate custom exception rather than letting PyJWT's errors escape.
    The API layer catches this one type and turns it into a 401; it does not
    need to know the twelve different ways PyJWT can object to a token.
    """


# A JWT (JSON Web Token) is three base64 chunks joined by dots:
# header.payload.signature. The payload holds "claims" — who the user is, when
# the token expires. Anyone can *read* it; that is not what makes it secure.
# The signature is what matters: only Supabase's private key can produce one
# that matches, so a valid signature proves Supabase issued this exact payload
# and nobody edited it in transit.
#
# PyJWKClient fetches the public keys from Supabase and caches them in memory,
# so this is not a network call on every request. `lifespan` is how long a
# cached key set is trusted before being re-fetched — which is what makes key
# rotation work without a redeploy.
_jwk_client = PyJWKClient(settings.supabase_jwks_url, cache_keys=True, lifespan=300)

# ES256 is what this project currently signs with; RS256 is accepted too in
# case the project's keys are ever rotated to RSA.
#
# This list is a security control, not a formality. Passing the algorithms the
# *token* claims to use would let an attacker pick "none" or downgrade to a
# symmetric algorithm and sign tokens themselves. Always state the algorithms
# you will accept.
_ALLOWED_ALGORITHMS = ["ES256", "RS256"]


def verify_access_token(token: str) -> dict:
    """Check a Supabase access token and return its claims.

    Raises TokenError if the token is anything other than genuine and current.

    Four things are checked, and all four matter:
      - the signature, against Supabase's published public key
      - `exp`, so an old token stops working
      - `aud` == "authenticated", Supabase's audience for a real logged-in user
      - `iss`, so a valid token from someone else's Supabase project is refused

    Returns the raw claims dict. `sub` is the user's uuid — the same value as
    auth.users.id, which is what user_account.id references.
    """
    try:
        # Reads the `kid` (key id) from the token header and returns the
        # matching public key, fetching the key set if the cache is cold.
        signing_key = _jwk_client.get_signing_key_from_jwt(token)

        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=_ALLOWED_ALGORITHMS,
            audience="authenticated",
            issuer=settings.supabase_issuer,
            # Tolerate a small clock difference between Supabase and this
            # machine.
            #
            # This is not theoretical politeness. Supabase's clock runs about a
            # second ahead of ours, and PyJWT refuses a token whose `iat` is in
            # the future. Without leeway, the very first request made with a
            # freshly minted token gets a 401 and every request after it
            # succeeds — which lands squarely on `POST /memoirs/claim`, the one
            # call the frontend makes the instant someone signs up.
            #
            # 30 seconds is the usual allowance: wide enough to absorb ordinary
            # drift between two servers, narrow enough that an expired token is
            # still refused promptly.
            leeway=30,
            # Refuse a token that omits either of these rather than treating a
            # missing claim as "fine". A token with no `exp` never expires.
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWKClientError as exc:
        # Could not get a key at all — usually the JWKS endpoint being
        # unreachable, or a `kid` we've never seen.
        logger.warning("Could not resolve a signing key for token: %s", exc)
        raise TokenError("could not verify token") from exc
    except jwt.InvalidTokenError as exc:
        # The catch-all parent of ExpiredSignatureError, InvalidAudienceError,
        # InvalidSignatureError and friends. Normal traffic, not a server fault
        # — log at info, not error.
        logger.info("Rejected token: %s", exc)
        raise TokenError("invalid token") from exc

    return claims


def password_signin(email: str, password: str) -> dict:
    """Exchange an email and password for a real Supabase session. DEV ONLY.

    This is the one place the API talks to Supabase Auth over HTTP rather than
    just verifying a token, and it exists purely so you can get a token with
    curl before the frontend login screen is wired up. In production the
    browser does this directly and the API never sees a password.

    Signs up first, then signs in. Signup on an existing email fails
    harmlessly; the sign-in that follows is what actually returns the session.
    """
    if not settings.supabase_anon_key:
        raise TokenError("SUPABASE_ANON_KEY is not set")

    base = settings.supabase_url.rstrip("/")
    headers = {
        "apikey": settings.supabase_anon_key,
        "Content-Type": "application/json",
    }
    body = {"email": email, "password": password}

    with httpx.Client(timeout=20) as client:
        # Best-effort account creation. A 4xx here means "already exists" (or
        # a weak password), both of which the sign-in below reports properly.
        signup = client.post(f"{base}/auth/v1/signup", json=body, headers=headers)
        logger.info("dev signup -> %s", signup.status_code)

        signin = client.post(
            f"{base}/auth/v1/token",
            params={"grant_type": "password"},
            json=body,
            headers=headers,
        )

    if signin.status_code != 200:
        # Surfaced verbatim so the cause is obvious while developing — most
        # often "Email not confirmed", which means email confirmation is still
        # switched on in the Supabase dashboard.
        raise TokenError(f"sign-in failed ({signin.status_code}): {signin.text}")

    return signin.json()
