"""Token verification, exercised with real signatures.

Nothing here mocks `verify_access_token`. The fixtures generate a genuine ES256
keypair and serve it where Supabase's JWKS would be, so every assertion below
runs the real function: the algorithm allow-list, the audience and issuer
checks, the required claims, the signature itself.

Mocking it would have been easier and would have tested nothing. This is the
one boundary where a passing test that does not exercise the real code is worse
than no test, because the failure it would hide is "anyone can mint a token".

None of these need a database. A bad token is refused before any handler runs.
"""

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from src.core.config import settings
from src.integrations.supabase_auth import TokenError, verify_access_token

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# The happy path, so the failures below mean something
# ---------------------------------------------------------------------------


def test_a_genuine_token_verifies(make_token):
    claims = verify_access_token(make_token(sub="user-1", email="a@example.test"))
    assert claims["sub"] == "user-1"
    assert claims["email"] == "a@example.test"


# ---------------------------------------------------------------------------
# Algorithm confusion — the classic JWT attack
# ---------------------------------------------------------------------------


def test_an_unsigned_token_is_refused(signing_key):
    """`alg: none` says "trust me, I checked".

    The attack is simple: strip the signature and set the algorithm to `none`.
    A library that takes the algorithm from the token's own header will happily
    accept it. This one is told which algorithms it will accept, in a hardcoded
    list, and `none` is not on it.
    """
    forged = jwt.encode(
        {"sub": "attacker", "aud": "authenticated", "iss": settings.supabase_issuer},
        key="",
        algorithm="none",
        headers={"kid": signing_key["kid"]},
    )

    with pytest.raises(TokenError):
        verify_access_token(forged)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _forge(header: dict, claims: dict, secret: bytes) -> str:
    """Assemble an HS256 token by hand.

    Necessary because PyJWT refuses to *encode* HS256 with a PEM key — a guard
    on the signing side. An attacker is not using PyJWT, so testing through it
    would prove only that its own guard works, not that our verifier is safe.
    """
    head = _b64(json.dumps(header).encode())
    body = _b64(json.dumps(claims).encode())
    signature = hmac.new(secret, f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(signature)}"


def test_a_symmetric_signature_over_the_public_key_is_refused(signing_key):
    """The subtler half of algorithm confusion, and the more dangerous one.

    Asymmetric verification expects a public key. If the library also accepts
    HS256, an attacker takes that *public* key — which is published; that is the
    entire point of it — uses it as an HMAC secret, and signs whatever they
    like. The verifier then computes an HMAC with the same public value and
    agrees. Anyone who can read the JWKS can mint tokens for any user.

    Refused here for one reason: `algorithms=["ES256", "RS256"]` is hardcoded,
    so the token's own claim to be HS256 is never honoured. Taking the algorithm
    from the token header is what makes this attack work, and this codebase
    never does.

    The forged token is assembled by hand and asserted to be well-formed first,
    so a failure to build it can never be mistaken for a passing test.
    """
    public_pem = signing_key["public"].public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    forged = _forge(
        {"alg": "HS256", "typ": "JWT", "kid": signing_key["kid"]},
        {
            "sub": "attacker",
            "aud": "authenticated",
            "iss": settings.supabase_issuer,
            "exp": int(time.time()) + 3600,
        },
        public_pem,
    )

    # It really is a syntactically valid HS256 token carrying a valid signature
    # over the published key — the attack is genuine, not a malformed string.
    assert jwt.get_unverified_header(forged)["alg"] == "HS256"
    assert len(forged.split(".")) == 3

    with pytest.raises(TokenError):
        verify_access_token(forged)


def test_a_token_signed_by_a_different_key_is_refused(make_token):
    """A real ES256 signature from a key that is not Supabase's."""
    attacker_key = ec.generate_private_key(ec.SECP256R1())

    with pytest.raises(TokenError):
        verify_access_token(make_token(key=attacker_key))


def test_an_unknown_key_id_is_refused(make_token):
    """A `kid` the JWKS has never published.

    The real client raises when it cannot resolve a key, and that is caught
    separately from a bad signature — both end as the same 401, which is the
    point.
    """
    with pytest.raises(TokenError):
        verify_access_token(make_token(kid="some-other-key"))


# ---------------------------------------------------------------------------
# Which project, and which audience
# ---------------------------------------------------------------------------


def test_a_token_from_another_supabase_project_is_refused(make_token):
    """Correctly signed, entirely valid — and not ours.

    Without the issuer check, anybody could stand up their own Supabase project,
    sign up, and present that project's token here. It would verify against
    *their* JWKS, not ours, so in practice the signature check catches it first
    — but the issuer check is what makes that a rule rather than an accident.
    """
    with pytest.raises(TokenError):
        verify_access_token(make_token(iss="https://someone-else.supabase.co/auth/v1"))


def test_the_wrong_audience_is_refused(make_token):
    """`aud` must be `authenticated` — Supabase's audience for a real login."""
    with pytest.raises(TokenError):
        verify_access_token(make_token(aud="anon"))


def test_a_token_with_no_audience_is_refused(make_token):
    """Absent is not the same as wrong, and both are refused."""
    with pytest.raises(TokenError):
        verify_access_token(make_token(aud=None))


# ---------------------------------------------------------------------------
# Expiry, and the thirty seconds that must not be removed
# ---------------------------------------------------------------------------


def test_an_expired_token_is_refused(make_token):
    with pytest.raises(TokenError):
        verify_access_token(make_token(exp_delta=timedelta(minutes=-5)))


def test_a_token_with_no_expiry_is_refused(make_token):
    """`options={"require": ["exp", "sub"]}` is doing this.

    Without it, a token with no `exp` is not expired — it is eternal. That is a
    far worse failure than a rejected login, and it is silent.
    """
    with pytest.raises(TokenError):
        verify_access_token(make_token(include_exp=False))


def test_a_token_missing_sub_is_refused(signing_key):
    """`sub` is read unguarded as `claims["sub"]` in the dependency.

    Required at decode time by `options={"require": [...]}`, so a token without
    it is a clean 401 rather than a KeyError and a 500 — the difference between
    refusing a caller and leaking a traceback.

    Built without `sub` rather than with `sub: None`, because a null value is
    still a present claim and would not exercise the requirement.
    """
    token = jwt.encode(
        {
            "aud": "authenticated",
            "iss": settings.supabase_issuer,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        signing_key["private"],
        algorithm="ES256",
        headers={"kid": signing_key["kid"]},
    )

    with pytest.raises(TokenError):
        verify_access_token(token)


def test_thirty_seconds_of_leeway_is_honoured(make_token):
    """The fix for a bug that looked like nothing else.

    Supabase's clock runs about a second ahead of ours, and a token issued in
    the future is refused by default. The symptom was bizarre: the *first*
    request made with a fresh token returned 401 and every one after it worked —
    landing exactly on `POST /memoirs/claim`, the call made the instant somebody
    signs up.

    A token that expired 20 seconds ago is still accepted; one that expired 40
    seconds ago is not. If somebody removes `leeway=30`, the first assertion
    here fails and says why.
    """
    just_expired = make_token(exp_delta=timedelta(seconds=-20))
    assert verify_access_token(just_expired)["sub"]

    with pytest.raises(TokenError):
        verify_access_token(make_token(exp_delta=timedelta(seconds=-40)))


# ---------------------------------------------------------------------------
# What the API does with all of that
# ---------------------------------------------------------------------------


def test_every_rejection_looks_identical_from_outside(client, make_token):
    """Expired, forged, wrong project, missing — one answer.

    Telling a caller *why* their token failed is free reconnaissance. Every
    failure returns the same 401 with the same body and the same
    `WWW-Authenticate` header.
    """
    attacker_key = ec.generate_private_key(ec.SECP256R1())

    bad_tokens = {
        "expired": make_token(exp_delta=timedelta(minutes=-5)),
        "forged": make_token(key=attacker_key),
        "wrong issuer": make_token(iss="https://elsewhere.supabase.co/auth/v1"),
        "wrong audience": make_token(aud="anon"),
        "unknown kid": make_token(kid="nope"),
        "not a jwt at all": "clearly-not-a-token",
    }

    seen = set()
    for label, token in bad_tokens.items():
        response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401, label
        seen.add((response.status_code, response.json()["detail"]))

    assert seen == {(401, "not authenticated")}, (
        "different rejection messages tell an attacker which part they got right"
    )


def test_a_missing_header_is_401_not_403(client):
    """401 means "I do not know who you are"; 403 means "I do, and no".

    `HTTPBearer(auto_error=False)` exists for this: its built-in error is a 403,
    which is the wrong answer to an absent credential and which the frontend
    reacts to differently — 401 shows the login screen.
    """
    response = client.get("/me")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_malformed_authorization_header_is_401(client):
    for header in ["", "Bearer", "Bearer ", "Basic dXNlcjpwYXNz", "token abc"]:
        response = client.get("/me", headers={"Authorization": header})
        assert response.status_code == 401, header


def test_rsa_tokens_are_accepted_so_key_rotation_does_not_break_login(monkeypatch):
    """RS256 is on the allow-list beside ES256, deliberately.

    The project signs with ES256 today. Accepting RS256 as well means rotating
    the Supabase keys to RSA would not take the API down — a real operational
    concern, not theoretical, and worth a test so nobody trims the list to one
    algorithm as a tidy-up.
    """
    from src.integrations import supabase_auth

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class _Key:
        key = rsa_key.public_key()

    class _Client:
        def get_signing_key_from_jwt(self, token):
            return _Key()

    monkeypatch.setattr(supabase_auth, "_jwk_client", _Client())

    from datetime import datetime, timezone

    token = jwt.encode(
        {
            "sub": "rsa-user",
            "aud": "authenticated",
            "iss": settings.supabase_issuer,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        rsa_key,
        algorithm="RS256",
    )

    assert verify_access_token(token)["sub"] == "rsa-user"


def test_dev_signin_is_not_registered(client):
    """`/dev/signin` mints a real token for anybody who asks.

    It is gated by `ENABLE_DEV_ROUTES`, and the gate is an import inside an `if`
    rather than a check inside the handler — so when it is off the module is
    never loaded, the route cannot appear in `/docs`, and it cannot be reached
    by guessing the path. There is no runtime branch to get wrong.

    The test environment sets the flag false, matching production.
    """
    response = client.post(
        "/dev/signin", json={"email": "a@example.test", "password": "x"}
    )
    assert response.status_code == 404
