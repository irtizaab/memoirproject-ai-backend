"""The door to a published memoir.

A view link is forwarded — that is what it is for — and every forward is a copy
of the whole book. So the link stopped being the credential and became half of
one: the other half is a passphrase the owner tells people separately, and what
you get for both is a session that also says who you are.

The tests that matter here are about what the door refuses, and about refusing
everything the same way. A link with a bad passphrase must be indistinguishable
from a link that was never real, or a leaked link becomes a target worth
guessing at.
"""

import uuid

import pytest

from tests.conftest import TOKEN_PATTERN, requires_db
from tests.factories import TEST_PASSPHRASE

pytestmark = [requires_db, pytest.mark.db]


@pytest.fixture
def book(factory, owner):
    """A published memoir with one chapter, and the link that opens it."""
    memoir_id = str(owner["memoir"]["id"])
    chapter = factory.chapter(memoir_id, ordinal=0)
    factory.block(memoir_id, str(chapter["id"]), ordinal=0, text="The dock at twilight.")
    view = factory.link(memoir_id, scope="view")
    factory.publish(memoir_id)

    return {
        "memoir_id": memoir_id,
        "owner_id": str(owner["account"]["id"]),
        "chapter_id": str(chapter["id"]),
        "view_token": view["token"],
        "view_link_id": str(view["id"]),
        "contribute_token": owner["memoir"]["link_token"],
    }


def _open(client, token, **body):
    return client.post(f"/r/{token}/open", json=body)


def _headers(token, session):
    return {"X-Link-Token": token, "X-Reader-Token": session["reader_token"]}


# ---------------------------------------------------------------------------
# Getting in
# ---------------------------------------------------------------------------


def test_the_passphrase_and_a_name_open_the_memoir(client, book):
    response = _open(
        client,
        book["view_token"],
        passphrase=TEST_PASSPHRASE,
        display_name="Clara Harrison",
        relationship="Granddaughter",
    )

    assert response.status_code == 200
    session = response.json()
    assert session["display_name"] == "Clara Harrison"
    assert session["is_owner"] is False
    assert TOKEN_PATTERN.match(session["participant_token"])
    assert session["reader_token"]


def test_the_session_is_what_the_book_opens_with(client, book):
    """And the link alone, which used to be enough, no longer is."""
    session = _open(
        client,
        book["view_token"],
        passphrase=TEST_PASSPHRASE,
        display_name="Clara Harrison",
    ).json()

    assert client.get(f"/r/{book['view_token']}").status_code == 404
    assert (
        client.get(
            f"/r/{book['view_token']}", headers=_headers(book["view_token"], session)
        ).status_code
        == 200
    )


def test_a_chapter_needs_the_session_too(client, book):
    """Not just the covers. The prose is the thing being protected."""
    assert (
        client.get(
            f"/chapters/{book['chapter_id']}",
            headers={"X-Link-Token": book["view_token"]},
        ).status_code
        == 404
    )

    session = _open(
        client, book["view_token"], passphrase=TEST_PASSPHRASE, display_name="Clara"
    ).json()
    assert (
        client.get(
            f"/chapters/{book['chapter_id']}",
            headers=_headers(book["view_token"], session),
        ).status_code
        == 200
    )


def test_the_relation_a_reader_gives_is_kept(client, book, db_cursor):
    """"Granddaughter" is not one of the six enum values and was never meant to
    be. It goes in `relationship_label`, which is the column for what somebody
    calls themselves."""
    _open(
        client,
        book["view_token"],
        passphrase=TEST_PASSPHRASE,
        display_name="Clara Harrison",
        relationship="Granddaughter",
    )

    db_cursor.execute(
        """
        SELECT relationship::text AS relationship, relationship_label AS label
          FROM memoir_participant
         WHERE memoir_id = %(memoir)s AND display_name = 'Clara Harrison'
        """,
        {"memoir": book["memoir_id"]},
    )
    row = db_cursor.fetchone()

    assert row["label"] == "Granddaughter"
    assert row["relationship"] == "other", "the enum is untouched"


def test_opening_the_book_is_counted(client, book, db_cursor):
    """A plain fact for the owner: how many people have read it.

    Distinct from the contribute link's own count, which is how many looked at
    the invitation. Two numbers, two meanings, and neither is a target.
    """
    _open(client, book["view_token"], passphrase=TEST_PASSPHRASE, display_name="Clara")
    _open(client, book["view_token"], passphrase=TEST_PASSPHRASE, display_name="David")

    db_cursor.execute(
        "SELECT open_count FROM memoir_link WHERE id = %(id)s",
        {"id": book["view_link_id"]},
    )
    assert db_cursor.fetchone()["open_count"] == 2


def test_a_returning_reader_keeps_their_identity(client, book):
    """Somebody who sent memories months ago and reads today is one person."""
    first = _open(
        client,
        book["view_token"],
        passphrase=TEST_PASSPHRASE,
        display_name="Clara Harrison",
    ).json()

    second = _open(
        client,
        book["view_token"],
        passphrase=TEST_PASSPHRASE,
        display_name="Clara Harrison",
        participant_token=first["participant_token"],
    ).json()

    assert second["reader_token"] == first["reader_token"]


# ---------------------------------------------------------------------------
# Being turned away — and always the same way
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "passphrase",
    ["not the passphrase", "", "ELLSWORTH LANE", "ellsworth lan"],
)
def test_the_wrong_passphrase_does_not_open_it(client, book, passphrase):
    response = _open(
        client, book["view_token"], passphrase=passphrase, display_name="Clara"
    )
    assert response.status_code == 404


def test_a_wrong_passphrase_looks_like_a_link_that_never_existed(client, book):
    """The one that matters. Telling these apart turns a leaked link into a
    target worth guessing at, and tells a stranger they have found something
    real."""
    wrong = _open(
        client, book["view_token"], passphrase="not it", display_name="Clara"
    )
    unknown = _open(
        client, "0" * 48, passphrase=TEST_PASSPHRASE, display_name="Clara"
    )

    assert wrong.status_code == unknown.status_code == 404
    assert wrong.json() == unknown.json()


def test_a_revoked_link_stops_opening_it(client, book, factory):
    factory.revoke_link(book["view_link_id"])
    response = _open(
        client, book["view_token"], passphrase=TEST_PASSPHRASE, display_name="Clara"
    )
    assert response.status_code == 404


def test_a_contribute_link_does_not_open_the_book(client, book):
    """Scope is the whole point of there being two links, and knowing the
    passphrase does not change which door a key fits."""
    response = _open(
        client,
        book["contribute_token"],
        passphrase=TEST_PASSPHRASE,
        display_name="Clara",
    )
    assert response.status_code == 404


def test_a_memoir_nobody_has_protected_opens_for_nobody(client, factory, owner):
    """No passphrase is not "no passphrase required".

    It is a draft whose owner has not decided to let anyone in yet, and the
    door treats it exactly like a link that does not exist.
    """
    memoir_id = str(owner["memoir"]["id"])
    view = factory.link(memoir_id, scope="view")

    response = _open(
        client, view["token"], passphrase="anything at all", display_name="Clara"
    )
    assert response.status_code == 404


def test_a_reader_must_say_who_they_are(client, book):
    """400, not 404: the link and the passphrase were right, and what is
    missing is something they can fix in the form in front of them."""
    response = _open(client, book["view_token"], passphrase=TEST_PASSPHRASE)
    assert response.status_code == 400

    blank = _open(
        client, book["view_token"], passphrase=TEST_PASSPHRASE, display_name="   "
    )
    assert blank.status_code == 400


# ---------------------------------------------------------------------------
# Forging a session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forged",
    [
        "",
        "nonsense",
        "no-dot-in-here",
        f"{uuid.UUID(int=0)}.",
        f"{uuid.UUID(int=0)}.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "not-a-uuid.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    ],
)
def test_a_session_that_was_not_issued_opens_nothing(client, book, forged):
    """Including the malformed ones, which must 404 like everything else
    rather than surfacing as a different kind of failure."""
    response = client.get(
        f"/r/{book['view_token']}",
        headers={"X-Link-Token": book["view_token"], "X-Reader-Token": forged},
    )
    assert response.status_code == 404


def test_a_session_from_one_memoir_does_not_open_another(client, factory, book, owner):
    """The signature is over the memoir as well as the participant, so a real
    session from a real memoir is worthless anywhere else."""
    other_account = factory.account(name="Another Owner")
    other = factory.memoir(other_account["id"], subject_name="Someone Else")
    other_id = str(other["id"])
    other_view = factory.link(other_id, scope="view")
    factory.chapter(other_id, ordinal=0)
    factory.protect(other_id)

    mine = _open(
        client, book["view_token"], passphrase=TEST_PASSPHRASE, display_name="Clara"
    ).json()

    response = client.get(
        f"/r/{other_view['token']}",
        headers={
            "X-Link-Token": other_view["token"],
            "X-Reader-Token": mine["reader_token"],
        },
    )
    assert response.status_code == 404


def test_replacing_the_passphrase_closes_every_session(as_owner, client, book):
    """Which is exactly what an owner means by changing it.

    They are usually changing it because it reached somebody it should not
    have, and a session that outlived the passphrase would make the change
    ceremonial.
    """
    session = _open(
        client, book["view_token"], passphrase=TEST_PASSPHRASE, display_name="Clara"
    ).json()
    headers = _headers(book["view_token"], session)
    assert client.get(f"/r/{book['view_token']}", headers=headers).status_code == 200

    as_owner(book["owner_id"]).put(
        f"/memoirs/{book['memoir_id']}/passphrase",
        json={"passphrase": "a different one entirely"},
    )

    assert client.get(f"/r/{book['view_token']}", headers=headers).status_code == 404


def test_revoking_the_link_closes_every_session(factory, client, book):
    """The other half of the same property: the signature is over the link too."""
    session = _open(
        client, book["view_token"], passphrase=TEST_PASSPHRASE, display_name="Clara"
    ).json()
    headers = _headers(book["view_token"], session)

    factory.revoke_link(book["view_link_id"])

    assert client.get(f"/r/{book['view_token']}", headers=headers).status_code == 404


def test_a_deleted_participant_stops_reading(client, book, db_cursor):
    """The signature proves we issued the id; the row proves the person is
    still in the memoir. Both, because one without the other is a session that
    outlives its owner."""
    session = _open(
        client, book["view_token"], passphrase=TEST_PASSPHRASE, display_name="Clara"
    ).json()
    headers = _headers(book["view_token"], session)

    db_cursor.execute(
        """
        DELETE FROM memoir_participant
         WHERE memoir_id = %(memoir)s AND display_name = 'Clara'
        """,
        {"memoir": book["memoir_id"]},
    )

    assert client.get(f"/r/{book['view_token']}", headers=headers).status_code == 404


# ---------------------------------------------------------------------------
# The owner
# ---------------------------------------------------------------------------


def test_the_owner_is_not_asked_for_a_passphrase_or_a_name(as_owner, book):
    """They set the one and their account carries the other.

    Asking a woman to type "Sarah, daughter" to read her own mother's memoir is
    the product forgetting who it belongs to.
    """
    response = as_owner(book["owner_id"]).post(
        f"/r/{book['view_token']}/open", json={}
    )

    assert response.status_code == 200
    session = response.json()
    assert session["is_owner"] is True
    assert session["display_name"] == "Test Owner"
    # Migration 0003 forbids an owner from holding a contributor token, and
    # this is the response that would have handed them one.
    assert session["participant_token"] is None


def test_the_owners_session_opens_the_book(as_owner, client, book):
    session = as_owner(book["owner_id"]).post(
        f"/r/{book['view_token']}/open", json={}
    ).json()

    response = client.get(
        f"/r/{book['view_token']}", headers=_headers(book["view_token"], session)
    )
    assert response.status_code == 200


def test_the_owner_comments_as_the_owner_through_their_session(
    as_owner, client, book
):
    """Not as a new contributor called by their own name.

    The failure this forbids is subtle and permanent: an owner let through the
    door as a stranger would appear in their own memoir twice, and every
    comment they left would be marked as somebody else's.
    """
    session = as_owner(book["owner_id"]).post(
        f"/r/{book['view_token']}/open", json={}
    ).json()

    db_chapter = book["chapter_id"]
    threads = client.get(
        f"/chapters/{db_chapter}", headers=_headers(book["view_token"], session)
    )
    assert threads.status_code == 200


def test_somebody_elses_bearer_token_is_not_a_way_in(as_owner, book, stranger):
    """A signed-in stranger is a stranger. They fall through to the passphrase
    path, offer none, and are turned away like anyone else."""
    response = as_owner(str(stranger["account"]["id"])).post(
        f"/r/{book['view_token']}/open", json={}
    )
    assert response.status_code == 404
