"""Sealing a memoir, and the passphrase that protects it.

Publishing is the most irreversible thing the product does. Afterwards the text
can never change, because every comment and every source in it is anchored to
character offsets that are only safe while it does not — so most of what is
tested here is what publishing *refuses*.

The passphrase tests are about one promise: the stored value is never the
passphrase, cannot be read back, and cannot be got around by sending something
malformed.
"""

import uuid

import pytest

from tests.conftest import requires_db
from tests.factories import TEST_PASSPHRASE

pytestmark = [requires_db, pytest.mark.db]


@pytest.fixture
def assembled(factory, owner):
    """A memoir with one chapter — the state publishing requires."""
    memoir_id = str(owner["memoir"]["id"])
    factory.chapter(memoir_id, ordinal=0)
    return {"memoir_id": memoir_id, "owner_id": str(owner["account"]["id"])}


# ---------------------------------------------------------------------------
# Sealing it
# ---------------------------------------------------------------------------


def test_publishing_seals_the_memoir_and_returns_the_link(as_owner, assembled):
    """The owner's one moment: a token to send, and a date it was finished."""
    response = as_owner(assembled["owner_id"]).post(
        f"/memoirs/{assembled['memoir_id']}/publish",
        json={"passphrase": TEST_PASSPHRASE},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["view_token"]
    assert body["published_at"]


def test_the_passphrase_is_never_echoed_back(as_owner, assembled):
    """It reached the server once. It does not travel back out.

    A passphrase in a response body is a passphrase in a browser cache, a proxy
    log and any error report that captures one.
    """
    response = as_owner(assembled["owner_id"]).post(
        f"/memoirs/{assembled['memoir_id']}/publish",
        json={"passphrase": TEST_PASSPHRASE},
    )

    assert TEST_PASSPHRASE not in response.text
    assert "passphrase" not in response.json()


def test_the_stored_value_is_not_the_passphrase(as_owner, assembled, db_cursor):
    """What lands in the database is a scrypt hash carrying its own parameters."""
    as_owner(assembled["owner_id"]).post(
        f"/memoirs/{assembled['memoir_id']}/publish",
        json={"passphrase": TEST_PASSPHRASE},
    )

    db_cursor.execute(
        "SELECT view_passphrase_hash AS h FROM memoir WHERE id = %(id)s",
        {"id": assembled["memoir_id"]},
    )
    stored = db_cursor.fetchone()["h"]

    assert TEST_PASSPHRASE not in stored
    assert stored.startswith("scrypt$16384$8$1$")


def test_publishing_issues_a_view_link_and_leaves_the_contribute_one(
    as_owner, assembled, db_cursor
):
    """Two links, two jobs. One collects memories, the other hands over the book.

    The contribute link is not revoked by publishing — `contributable_memoir`
    already refuses it on a published memoir, so killing the row as well would
    lose the address without changing the behaviour.
    """
    as_owner(assembled["owner_id"]).post(
        f"/memoirs/{assembled['memoir_id']}/publish",
        json={"passphrase": TEST_PASSPHRASE},
    )

    db_cursor.execute(
        """
        SELECT scope::text AS scope FROM memoir_link
         WHERE memoir_id = %(id)s AND revoked_at IS NULL
         ORDER BY scope
        """,
        {"id": assembled["memoir_id"]},
    )
    assert [r["scope"] for r in db_cursor.fetchall()] == ["contribute", "view"]


def test_a_view_link_sent_before_publication_keeps_its_address(
    as_owner, factory, assembled
):
    """An owner who showed one person early does not have their link swapped.

    The partial unique index permits one live link per scope, so publishing
    looks before it inserts.
    """
    existing = factory.link(assembled["memoir_id"], scope="view")

    body = as_owner(assembled["owner_id"]).post(
        f"/memoirs/{assembled['memoir_id']}/publish",
        json={"passphrase": TEST_PASSPHRASE},
    ).json()

    assert body["view_token"] == existing["token"]


# ---------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------


def test_an_unassembled_memoir_cannot_be_published(as_owner, owner):
    """Sealing is one-way, and spending it on an empty book helps nobody."""
    response = as_owner(str(owner["account"]["id"])).post(
        f"/memoirs/{owner['memoir']['id']}/publish",
        json={"passphrase": TEST_PASSPHRASE},
    )

    assert response.status_code == 400
    assert "assemble" in response.json()["detail"]


def test_a_memoir_is_not_published_twice(as_owner, assembled):
    """409. Forgetting the passphrase is not a reason to republish."""
    client = as_owner(assembled["owner_id"])
    client.post(
        f"/memoirs/{assembled['memoir_id']}/publish",
        json={"passphrase": TEST_PASSPHRASE},
    )

    again = client.post(
        f"/memoirs/{assembled['memoir_id']}/publish",
        json={"passphrase": "a different one"},
    )

    assert again.status_code == 409


def test_a_short_passphrase_is_refused(as_owner, assembled):
    """422 from the model, before anything is hashed or written."""
    response = as_owner(assembled["owner_id"]).post(
        f"/memoirs/{assembled['memoir_id']}/publish",
        json={"passphrase": "short"},
    )

    assert response.status_code == 422


def test_a_passphrase_of_spaces_is_refused(as_owner, assembled):
    """Long enough for the model, empty once stripped.

    This is the case `hash_passphrase` exists to catch: Pydantic counts
    characters and the KDF is the last thing between a memoir and a password of
    nothing.
    """
    response = as_owner(assembled["owner_id"]).post(
        f"/memoirs/{assembled['memoir_id']}/publish",
        json={"passphrase": "          "},
    )

    assert response.status_code == 400
    assert "at least" in response.json()["detail"]


def test_publishing_leaves_no_half_sealed_memoir(as_owner, assembled, db_cursor):
    """A refused publish changes nothing at all.

    Status, timestamp and passphrase move in one statement inside one
    transaction. The state this guards against — sealed with no passphrase — is
    also refused by `memoir_published_is_protected`, and both are worth having.
    """
    as_owner(assembled["owner_id"]).post(
        f"/memoirs/{assembled['memoir_id']}/publish",
        json={"passphrase": "     "},
    )

    db_cursor.execute(
        """
        SELECT status::text AS status, published_at, view_passphrase_hash AS h
          FROM memoir WHERE id = %(id)s
        """,
        {"id": assembled["memoir_id"]},
    )
    row = db_cursor.fetchone()

    assert row["status"] == "draft"
    assert row["published_at"] is None
    assert row["h"] is None


# ---------------------------------------------------------------------------
# Whose memoir it is
# ---------------------------------------------------------------------------


def test_a_stranger_cannot_publish_someone_elses_memoir(as_owner, stranger, assembled):
    """404, never 403."""
    response = as_owner(str(stranger["account"]["id"])).post(
        f"/memoirs/{assembled['memoir_id']}/publish",
        json={"passphrase": TEST_PASSPHRASE},
    )

    assert response.status_code == 404


def test_a_stranger_cannot_change_someone_elses_passphrase(
    as_owner, stranger, assembled, db_cursor
):
    """The worst version of this bug: locking a family out of their own memoir."""
    as_owner(assembled["owner_id"]).post(
        f"/memoirs/{assembled['memoir_id']}/publish",
        json={"passphrase": TEST_PASSPHRASE},
    )
    db_cursor.execute(
        "SELECT view_passphrase_hash AS h FROM memoir WHERE id = %(id)s",
        {"id": assembled["memoir_id"]},
    )
    before = db_cursor.fetchone()["h"]

    response = as_owner(str(stranger["account"]["id"])).put(
        f"/memoirs/{assembled['memoir_id']}/passphrase",
        json={"passphrase": "not yours to set"},
    )

    db_cursor.execute(
        "SELECT view_passphrase_hash AS h FROM memoir WHERE id = %(id)s",
        {"id": assembled["memoir_id"]},
    )

    assert response.status_code == 404
    assert db_cursor.fetchone()["h"] == before


def test_publishing_a_memoir_that_does_not_exist_answers_the_same_way(
    as_owner, assembled
):
    response = as_owner(assembled["owner_id"]).post(
        f"/memoirs/{uuid.uuid4()}/publish",
        json={"passphrase": TEST_PASSPHRASE},
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Replacing it
# ---------------------------------------------------------------------------


def test_the_passphrase_can_be_replaced(as_owner, assembled, db_cursor):
    """The only move available, because the old one cannot be read back."""
    client = as_owner(assembled["owner_id"])
    client.post(
        f"/memoirs/{assembled['memoir_id']}/publish",
        json={"passphrase": TEST_PASSPHRASE},
    )
    db_cursor.execute(
        "SELECT view_passphrase_hash AS h FROM memoir WHERE id = %(id)s",
        {"id": assembled["memoir_id"]},
    )
    before = db_cursor.fetchone()["h"]

    response = client.put(
        f"/memoirs/{assembled['memoir_id']}/passphrase",
        json={"passphrase": "the cedar planks"},
    )

    db_cursor.execute(
        "SELECT view_passphrase_hash AS h FROM memoir WHERE id = %(id)s",
        {"id": assembled["memoir_id"]},
    )

    assert response.status_code == 204
    assert db_cursor.fetchone()["h"] != before


def test_a_draft_can_be_protected_before_it_is_sealed(as_owner, owner, db_cursor):
    """For the owner who sends a view link to one person early."""
    response = as_owner(str(owner["account"]["id"])).put(
        f"/memoirs/{owner['memoir']['id']}/passphrase",
        json={"passphrase": TEST_PASSPHRASE},
    )

    db_cursor.execute(
        "SELECT status::text AS status, view_passphrase_hash AS h FROM memoir "
        "WHERE id = %(id)s",
        {"id": owner["memoir"]["id"]},
    )
    row = db_cursor.fetchone()

    assert response.status_code == 204
    assert row["status"] == "draft"
    assert row["h"] is not None


# ---------------------------------------------------------------------------
# What the dashboard reads
# ---------------------------------------------------------------------------


def test_the_account_carries_the_view_token_after_publication(as_owner, assembled):
    """`/me` is where the dashboard learns there is a book and how to open it.

    Null before, real after — and never the passphrase hash, which this model
    does not declare.
    """
    client = as_owner(assembled["owner_id"])

    before = client.get("/me").json()["memoirs"][0]
    assert before["view_token"] is None
    assert before["published_at"] is None
    assert before["status"] == "draft"

    published = client.post(
        f"/memoirs/{assembled['memoir_id']}/publish",
        json={"passphrase": TEST_PASSPHRASE},
    ).json()

    after = client.get("/me").json()["memoirs"][0]
    assert after["view_token"] == published["view_token"]
    assert after["published_at"] is not None
    assert after["status"] == "published"
    assert "view_passphrase_hash" not in after
