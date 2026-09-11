# Hashing the one password this product has.
#
# Not an integration: nothing external is called. Not `core/` either, because
# only memoirs have a passphrase. It sits beside `access.py` for the same
# reason that file exists — it answers "may this person open this memoir", and
# the answer has to be given identically everywhere it is asked.
#
# ---------------------------------------------------------------------------
# Why scrypt out of the standard library
# ---------------------------------------------------------------------------
# A passphrase is chosen by a person and told to relatives over the phone, so
# it is short, memorable, and guessable by anyone who gets the link. The only
# defence that matters is making each guess expensive, which is what a memory-
# hard KDF is for. `hashlib.scrypt` is that, it ships with Python, and it needs
# no wheel that has to build on a laptop.
#
# passlib and bcrypt would each have been a dependency added to a repository
# that has none for this, to do a thing the standard library already does.
#
# ---------------------------------------------------------------------------
# The parameters, and why they are stored
# ---------------------------------------------------------------------------
# n=16384, r=8, p=1 is the interactive-login setting from the scrypt paper:
# roughly 16 MB and a few tens of milliseconds per attempt. That is unnoticeable
# on one open and ruinous at scale, which is the whole trade.
#
# They are written into the stored value rather than assumed, so raising them
# later does not invalidate every existing row. `verify` reads whatever numbers
# a row was written with; `needs_rehash` says when a value is behind, and the
# open path rewrites it at the next successful use — the one moment the
# plaintext is in hand.

import base64
import hmac
import logging
import secrets
from hashlib import scrypt

logger = logging.getLogger(__name__)

SCHEME = "scrypt"

# Cost. Raise `N` (always a power of two) to make every guess more expensive;
# anything already stored keeps verifying with its own numbers.
N = 16384
R = 8
P = 1

SALT_BYTES = 16
KEY_BYTES = 32


class PassphraseTooShort(Exception):
    """The owner chose something that is not worth hashing.

    Eight characters, checked here rather than only in Pydantic, because this
    function is the last thing between a memoir and a passphrase of "a". The
    API layer turns it into a 400 with the rule in it.
    """


MINIMUM_LENGTH = 8


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=KEY_BYTES,
        maxmem=128 * n * r * 2,
    )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_passphrase(passphrase: str) -> str:
    """Encode a passphrase for storage. Never reversible, never logged.

    Returns `scrypt$n$r$p$salt$key`, all base64 where it is bytes.

    The passphrase is stripped of surrounding whitespace first and nothing
    else. Case is kept: "Ellsworth" and "ellsworth" are different passphrases,
    because normalising would quietly halve the space of a value people already
    choose badly.
    """
    passphrase = passphrase.strip()
    if len(passphrase) < MINIMUM_LENGTH:
        raise PassphraseTooShort

    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(passphrase, salt, N, R, P)
    return f"{SCHEME}${N}${R}${P}${_b64(salt)}${_b64(key)}"


def verify_passphrase(passphrase: str, encoded: str | None) -> bool:
    """Whether this passphrase produces the stored value.

    False for everything that is not a match, including a memoir with no
    passphrase at all and a stored value this code cannot parse. A caller must
    never be able to open a memoir *because* something was malformed.

    `compare_digest` rather than `==` — the same idiom `api/webhooks.py`
    already uses. The timing difference on a 32-byte comparison is small, and
    the reason to use it anyway is that it costs nothing and the alternative
    has to be argued about every time somebody reads it.
    """
    if not encoded or not passphrase:
        return False

    try:
        scheme, n, r, p, salt_b64, key_b64 = encoded.split("$")
        if scheme != SCHEME:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(key_b64)
        candidate = _derive(passphrase.strip(), salt, int(n), int(r), int(p))
    except (ValueError, TypeError):
        # A stored value that cannot be parsed is a bug, not a credential.
        # Logged without the value itself: it is a password hash.
        logger.warning("Unreadable passphrase hash on a memoir")
        return False

    return hmac.compare_digest(candidate, expected)


def needs_rehash(encoded: str | None) -> bool:
    """Whether a stored value was written with weaker parameters than today's.

    Called on the way through a successful open, which is the only moment the
    plaintext exists to rewrite it with.
    """
    if not encoded:
        return False
    try:
        scheme, n, r, p, _salt, _key = encoded.split("$")
    except ValueError:
        return False
    return scheme != SCHEME or (int(n), int(r), int(p)) != (N, R, P)
