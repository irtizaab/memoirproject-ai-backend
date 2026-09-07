# The door to a published memoir, and who is standing at it.
#
# Nothing here imports fastapi. It returns None or raises its own exceptions;
# api/chapters.py decides which status code each one is.
#
# ---------------------------------------------------------------------------
# Two things happen at the door, and they are the same event on purpose
# ---------------------------------------------------------------------------
# A person arriving with a view link is asked for the passphrase and for their
# name. Not because the name is a credential — it proves nothing — but because
# this is the moment the product has always needed it and never had a place to
# ask. Comments used to collect a name at the point of writing one, which meant
# somebody could read a whole memoir as nobody at all and be asked who they
# were only if they had something to say.
#
# Asking once, at the door, is also what makes the reflection layer honest:
# every comment in the book is signed, because there is no way in that does not
# involve saying who you are.
#
# The owner is the exception, and only for the name: they are already named by
# their account, and asking a woman to type "Sarah, daughter" to read her own
# mother's memoir would be the product forgetting who it belongs to.
#
# ---------------------------------------------------------------------------
# Why the session is signed rather than stored
# ---------------------------------------------------------------------------
# What comes back is `{participant_id}.{signature}` — not a row. Three reasons,
# and the third is the one that decided it:
#
#   1. No table, no column, no migration, and nothing to clean up. A reader
#      session is derivable, so storing it would be storing a fact twice.
#
#   2. `memoir_participant.contributor_token` cannot carry it. A CHECK from
#      migration 0003 forbids an owner from having one — deliberately, so the
#      owner never holds a second, weaker credential than their account — and
#      the owner has to be able to read their own memoir.
#
#   3. The signing key is the memoir's own secrets: the live view link token
#      and the passphrase hash. So replacing the passphrase, or revoking and
#      reissuing the link, invalidates every session that was ever handed out.
#      That is exactly what an owner means when they change the passphrase
#      because it reached someone it should not have — and a stored token
#      would have needed a sweep to do the same thing.

import base64
import hmac
import logging
from hashlib import sha256
from uuid import UUID

from src.domain.memories.memory_service import resolve_participant
from src.domain.memoirs.passphrase import (
    hash_passphrase,
    needs_rehash,
    verify_passphrase,
)
from src.integrations.db import db

logger = logging.getLogger(__name__)


class ReaderNameRequired(Exception):
    """Somebody tried to open a memoir without saying who they are.

    Only reachable on the link path. The owner is named by their token; anybody
    else has nothing else identifying them, and an unsigned reflection in a
    family memoir is worse than no reflection — the whole product is about who
    said what.
    """


# ---------------------------------------------------------------------------
# The signature
# ---------------------------------------------------------------------------


def _sign(memoir_id: str, participant_id: str, key: str) -> str:
    digest = hmac.new(
        key.encode("utf-8"),
        f"{memoir_id}:{participant_id}".encode("utf-8"),
        sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _key(link_token: str, passphrase_hash: str | None) -> str:
    """The per-memoir signing key.

    Both halves matter. The passphrase hash means changing the passphrase logs
    everyone out; the link token means revoking the link does too. A memoir
    with no passphrase — a draft the owner is previewing — is signed with the
    link token alone, and forging a session for one would need that token *and*
    a participant id, which nothing hands out to somebody who cannot already
    read the memoir.
    """
    return f"{link_token}:{passphrase_hash or ''}"


def issue(memoir_id: str, participant_id: str, link_token: str, passphrase_hash: str | None) -> str:
    return f"{participant_id}.{_sign(memoir_id, participant_id, _key(link_token, passphrase_hash))}"


# ---------------------------------------------------------------------------
# Opening it
# ---------------------------------------------------------------------------


async def _memoir_at(cur, link_token: str) -> dict | None:
    """The memoir a live view link points at, with the hash to check against.

    A deliberate near-duplicate of `access.readable_memoir`, which does not
    select `view_passphrase_hash` and should not start: every other caller of
    it puts its result somewhere on the way to a response, and a password hash
    is not a thing to have in hand when that is what you are doing.
    """
    await cur.execute(
        """
        SELECT m.id, m.view_passphrase_hash, m.created_by_user_id
          FROM memoir_link l
          JOIN memoir m ON m.id = l.memoir_id
         WHERE l.token = %(token)s
           AND l.revoked_at IS NULL
           AND l.scope = 'view'
        """,
        {"token": link_token},
    )
    return await cur.fetchone()


async def open_for_reading(
    link_token: str,
    *,
    passphrase: str | None,
    display_name: str | None,
    relationship: str | None,
    participant_token: str | None,
    user_id: str | None,
) -> dict | None:
    """Exchange a passphrase and a name for a reader session.

    Returns `{reader_token, display_name, is_owner, participant_token}`, or
    None if this link and passphrase do not open anything. Raises
    ReaderNameRequired when somebody who is not the owner gives no name.

    Everything that fails returns None and the API answers 404 for all of it:
    an unknown token, a revoked one, a contribute link, a memoir with no
    passphrase set, and the wrong passphrase are one response. A caller must
    not be able to tell a real link with a bad passphrase from a link that was
    never real — that difference is what turns a leaked link into a target.

    The owner is recognised by their bearer token and let straight through. No
    passphrase, because they set it and may not have it to hand; no name,
    because their account already carries one.
    """
    async with db() as conn, conn.cursor() as cur:
        memoir = await _memoir_at(cur, link_token)
        if memoir is None:
            return None

        memoir_id = str(memoir["id"])
        stored = memoir["view_passphrase_hash"]

        # --- the owner, arriving at their own book -------------------------
        if user_id is not None and str(memoir["created_by_user_id"]) == user_id:
            await cur.execute(
                """
                SELECT id, display_name FROM memoir_participant
                 WHERE memoir_id = %(memoir)s AND role = 'owner'
                """,
                {"memoir": memoir_id},
            )
            owner = await cur.fetchone()
            return {
                "reader_token": issue(memoir_id, str(owner["id"]), link_token, stored),
                "display_name": owner["display_name"],
                "is_owner": True,
                # None, and the CHECK in migration 0003 is why: an owner never
                # holds a contributor token.
                "participant_token": None,
            }

        # --- everybody else ------------------------------------------------
        #
        # A memoir with no passphrase cannot be opened by link at all. That is
        # not an oversight to be forgiving about: it is a draft whose owner has
        # not decided to let anyone in yet.
        if not verify_passphrase(passphrase or "", stored):
            logger.info("A reader offered the wrong passphrase")
            return None

        name = (display_name or "").strip()
        if not name:
            raise ReaderNameRequired

        participant = await resolve_participant(
            cur,
            memoir_id=memoir_id,
            token=participant_token,
            display_name=name,
        )
        participant_id = str(participant["id"])

        # The relation, as they gave it: "Granddaughter", "Cousin David's wife".
        # It goes in `relationship_label` and not in `relationship`, which is a
        # database enum of six values and could not hold it. Written only when
        # they said something, so a returning contributor who leaves it blank
        # keeps what they said last time.
        told = (relationship or "").strip()
        if told:
            await cur.execute(
                """
                UPDATE memoir_participant
                   SET relationship_label = %(label)s
                 WHERE memoir_id = %(memoir)s
                   AND id = %(participant)s
                   AND coalesce(relationship_label, '') <> %(label)s
                """,
                {"label": told, "memoir": memoir_id, "participant": participant_id},
            )

        # Opening the book is what a view link's open_count counts. The
        # contribute link counts its own opens in domain/links/, and the two
        # numbers mean different things to an owner: how many people looked at
        # the invitation, and how many people have read the memoir.
        await cur.execute(
            """
            UPDATE memoir_link
               SET open_count = open_count + 1
             WHERE token = %(token)s
            """,
            {"token": link_token},
        )

        # The one moment the plaintext exists to rewrite a hash with. Raising
        # the work factor later therefore costs nothing and happens quietly,
        # one reader at a time.
        if needs_rehash(stored):
            await cur.execute(
                "UPDATE memoir SET view_passphrase_hash = %(hash)s WHERE id = %(id)s",
                {"hash": hash_passphrase(passphrase or ""), "id": memoir_id},
            )

        return {
            "reader_token": issue(memoir_id, participant_id, link_token, stored),
            "display_name": name,
            "is_owner": False,
            # Handed back so the same browser is the same person on the
            # contribute side too — somebody who sent memories months ago and
            # reads today must not appear in the memoir twice.
            "participant_token": participant["contributor_token"],
        }


# ---------------------------------------------------------------------------
# Checking it, on every request after the first
# ---------------------------------------------------------------------------


async def reader_participant(cur, memoir: dict, link_token: str, reader_token: str | None) -> str | None:
    """The participant this session belongs to, or None if it does not hold.

    Takes the memoir row a caller has already resolved, so the check composes
    inside their transaction rather than opening a second one — the same shape
    `domain/memoirs/access.py` uses and for the same reason.

    Every failure is None: no session, a malformed one, a signature that does
    not verify, and a participant that has since been deleted. The caller turns
    that into 404, as it does for every other "you cannot reach this".
    """
    if not reader_token or "." not in reader_token:
        return None

    participant_id, _, signature = reader_token.partition(".")
    memoir_id = str(memoir["id"])

    # Before the id reaches a uuid column. Postgres answers a malformed one
    # with 22P02, which `error_handlers.py` turns into a 400 — and a session
    # that is simply wrong must look like every other wrong session, not like a
    # different kind of failure.
    try:
        UUID(participant_id)
    except ValueError:
        return None

    await cur.execute(
        "SELECT view_passphrase_hash AS h FROM memoir WHERE id = %(id)s",
        {"id": memoir_id},
    )
    stored = (await cur.fetchone())["h"]

    expected = _sign(memoir_id, participant_id, _key(link_token, stored))
    if not hmac.compare_digest(signature, expected):
        return None

    # The signature proves the id was issued by us for this memoir. This proves
    # the row is still there — a participant merged away or deleted since
    # should not keep reading on a session minted before it happened.
    await cur.execute(
        """
        SELECT id FROM memoir_participant
         WHERE memoir_id = %(memoir)s AND id = %(participant)s
        """,
        {"memoir": memoir_id, "participant": participant_id},
    )
    return participant_id if await cur.fetchone() else None
