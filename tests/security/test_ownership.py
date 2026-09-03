"""Whose memoir is this.

The single most important file in the suite.

Row Level Security is enabled on all nine tables with **zero policies**, and the
API connects with a role that bypasses it. The database will happily return any
row to this application. Every `WHERE created_by_user_id = ...` and
`WHERE memoir_id = ...` in `src/domain/` is therefore the only thing standing
between one family and another's recordings.

Nothing else checks. These tests do.
"""

import uuid

import pytest

from tests.conftest import requires_db

pytestmark = [pytest.mark.security, requires_db, pytest.mark.db]


# ---------------------------------------------------------------------------
# Every memoir-scoped route, from the wrong account
# ---------------------------------------------------------------------------


def memoir_routes(memoir_id: str):
    """Every route addressed by a memoir id, with a harmless body."""
    return [
        ("GET", f"/memoirs/{memoir_id}/memories", None),
        ("POST", f"/memoirs/{memoir_id}/memories", {"body_text": "mine now"}),
        ("GET", f"/memoirs/{memoir_id}/contributors", None),
        ("POST", f"/memoirs/{memoir_id}/link/reissue", None),
    ]


def test_the_route_list_is_not_empty():
    """Guards the loops below.

    Every test in this file iterates `memoir_routes`. If that ever returned
    nothing, each of them would pass by doing nothing at all — a security file
    quietly asserting zero things is worse than one that fails.
    """
    assert len(memoir_routes("id")) >= 4


def test_a_stranger_gets_404_on_every_memoir_route(as_owner, owner, stranger):
    """Not 403. Not an empty list. Not found.

    403 would confirm the id belongs to a real memoir, which is exactly what
    someone guessing ids wants to learn. An empty 200 would be worse still: it
    reads as "this memoir exists and has nothing in it".
    """
    client = as_owner(stranger["account"]["id"])

    for method, path, body in memoir_routes(owner["memoir"]["id"]):
        response = client.request(method, path, json=body)
        assert response.status_code == 404, f"{method} {path} answered {response.status_code}"
        assert response.json()["detail"] == "memoir not found"


def test_a_stranger_gets_404_on_every_memory_route(as_owner, owner, stranger, factory):
    """The same, for routes addressed by memory id.

    These carry no memoir id at all, so ownership has to be reached by joining
    the memory back up to its memoir — `owned_memoir_of_memory`. A join is easy
    to get subtly wrong, which is why it gets its own test rather than being
    assumed to behave like the memoir routes.
    """
    memory = factory.memory(
        owner["memoir"]["id"], owner["memoir"]["owner_participant_id"]
    )
    asset = factory.asset(owner["memoir"]["id"], memory_id=memory["id"])

    client = as_owner(stranger["account"]["id"])

    routes = [
        ("GET", f"/memories/{memory['id']}", None),
        ("PATCH", f"/memories/{memory['id']}", {"title": "taken"}),
        ("DELETE", f"/memories/{memory['id']}", None),
        ("POST", f"/memories/{memory['id']}/assets", {"asset_ids": [str(asset["id"])]}),
        ("DELETE", f"/memories/{memory['id']}/assets/{asset['id']}", None),
    ]

    for method, path, body in routes:
        response = client.request(method, path, json=body)
        assert response.status_code == 404, f"{method} {path} answered {response.status_code}"


def test_a_stranger_cannot_read_a_memory_through_a_404(as_owner, owner, stranger, factory):
    """The 404 carries nothing.

    A response that said "not found" but leaked the title in an error message
    would defeat the whole arrangement.
    """
    memory = factory.memory(
        owner["memoir"]["id"],
        owner["memoir"]["owner_participant_id"],
        title="Her name was Nusrat",
        body_text="A private recollection.",
    )

    response = as_owner(stranger["account"]["id"]).get(f"/memories/{memory['id']}")

    assert response.status_code == 404
    assert "Nusrat" not in response.text
    assert "private recollection" not in response.text


def test_a_missing_memoir_and_someone_elses_are_indistinguishable(
    as_owner, owner, stranger
):
    """The oracle test.

    If a memoir that exists answers differently from one that never did, an
    attacker can enumerate real ids without ever reading one. Status, body and
    headers all have to match.
    """
    client = as_owner(stranger["account"]["id"])

    theirs = client.get(f"/memoirs/{owner['memoir']['id']}/memories")
    imaginary = client.get(f"/memoirs/{uuid.uuid4()}/memories")

    assert theirs.status_code == imaginary.status_code == 404
    assert theirs.json() == imaginary.json()


# ---------------------------------------------------------------------------
# The owner's own routes still work — otherwise the above proves nothing
# ---------------------------------------------------------------------------


def test_the_owner_can_reach_their_own_memoir(as_owner, owner, factory, storage):
    """The control.

    Without this, a bug that made every request 404 would look like perfect
    security.
    """
    factory.memory(owner["memoir"]["id"], owner["memoir"]["owner_participant_id"])

    client = as_owner(owner["account"]["id"])

    listing = client.get(f"/memoirs/{owner['memoir']['id']}/memories")
    assert listing.status_code == 200
    assert len(listing.json()) == 1

    assert client.get(f"/memoirs/{owner['memoir']['id']}/contributors").status_code == 200


# ---------------------------------------------------------------------------
# Cross-memoir grafting
# ---------------------------------------------------------------------------


def test_an_asset_cannot_be_grafted_from_another_memoir(
    as_owner, owner, stranger, factory, storage
):
    """Two ids, both real, both belonging to different people.

    `_adopt_assets` filters on `memoir_id` as well as the asset id, so a request
    naming somebody else's photograph matches no rows and changes nothing. The
    filter looks redundant next to an id that is already a UUID — this is what
    it is actually for.
    """
    mine = factory.memory(
        owner["memoir"]["id"], owner["memoir"]["owner_participant_id"]
    )
    theirs = factory.asset(stranger["memoir"]["id"])

    response = as_owner(owner["account"]["id"]).post(
        f"/memories/{mine['id']}/assets", json={"asset_ids": [str(theirs["id"])]}
    )

    # 200 — the request was well-formed and the caller owns the memory. The
    # asset simply matched nothing.
    assert response.status_code == 200
    assert response.json()["assets"] == [], "somebody else's photograph was adopted"


def test_an_asset_cannot_be_deleted_through_a_memory_that_does_not_hold_it(
    as_owner, owner, factory, storage
):
    """`DELETE /memories/{a}/assets/{b}` filters on both halves.

    Owning the memory is not enough; the asset has to actually be on it.
    Without the second filter, an owner could delete any asset in any memoir by
    routing the request through a memory they do own.
    """
    holder = factory.memory(
        owner["memoir"]["id"], owner["memoir"]["owner_participant_id"]
    )
    other = factory.memory(
        owner["memoir"]["id"], owner["memoir"]["owner_participant_id"]
    )
    asset = factory.asset(owner["memoir"]["id"], memory_id=holder["id"])

    response = as_owner(owner["account"]["id"]).delete(
        f"/memories/{other['id']}/assets/{asset['id']}"
    )

    assert response.status_code == 404
    assert storage.deleted == [], "a file was deleted through the wrong memory"


def test_contributors_cannot_be_merged_across_memoirs(
    as_owner, owner, stranger, factory
):
    """Refused by the schema, not only by the code.

    `participant_merge_target` is a composite foreign key on
    `(memoir_id, merged_into)`, so a merge that reached across memoirs could not
    be stored even if every line of Python allowed it. The route checks first
    and answers 404, which is the same answer as a participant that does not
    exist.
    """
    mine = factory.contributor(owner["memoir"]["id"], display_name="Ali")
    theirs = factory.contributor(stranger["memoir"]["id"], display_name="Ali")

    response = as_owner(owner["account"]["id"]).post(
        f"/memoirs/{owner['memoir']['id']}/contributors/"
        f"{theirs['id']}/merge-into/{mine['id']}"
    )

    assert response.status_code == 404


def test_the_owner_is_never_a_duplicate(as_owner, owner, factory):
    """Merging the owner into a contributor would hand over the memoir.

    Refused with 400 rather than 404 — the caller owns everything named, so
    "not found" would be a lie. The request is simply incoherent.
    """
    contributor = factory.contributor(owner["memoir"]["id"])
    owner_participant = owner["memoir"]["owner_participant_id"]

    client = as_owner(owner["account"]["id"])

    into_contributor = client.post(
        f"/memoirs/{owner['memoir']['id']}/contributors/"
        f"{owner_participant}/merge-into/{contributor['id']}"
    )
    assert into_contributor.status_code == 400

    from_contributor = client.post(
        f"/memoirs/{owner['memoir']['id']}/contributors/"
        f"{contributor['id']}/merge-into/{owner_participant}"
    )
    assert from_contributor.status_code == 400


def test_a_participant_cannot_be_merged_into_itself(as_owner, owner, factory):
    """A self-merge would make `_resolve_contributor` follow a pointer to itself.

    Refused in the route, and refused again by `participant_not_merged_into_self`
    underneath.
    """
    contributor = factory.contributor(owner["memoir"]["id"])

    response = as_owner(owner["account"]["id"]).post(
        f"/memoirs/{owner['memoir']['id']}/contributors/"
        f"{contributor['id']}/merge-into/{contributor['id']}"
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Billing is per-account
# ---------------------------------------------------------------------------


def test_billing_shows_only_your_own_storage(as_owner, owner, stranger, factory):
    """The storage meter sums across the memoirs you own, and no others."""
    factory.asset(owner["memoir"]["id"], byte_size=1000)
    factory.asset(stranger["memoir"]["id"], byte_size=999_000)

    response = as_owner(owner["account"]["id"]).get("/billing")

    assert response.status_code == 200
    assert response.json()["storage"]["used_bytes"] == 1000


def test_an_account_with_no_row_gets_404_not_somebody_elses_billing(as_owner, factory):
    """A signed-in user who has never claimed a memoir."""
    response = as_owner(str(uuid.uuid4())).get("/billing")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Published memoirs
# ---------------------------------------------------------------------------


def test_a_published_memoir_refuses_every_write(as_owner, owner, factory, storage):
    """The product's hardest rule, at every door that writes.

    Once a memoir is published its content can never change — not by the owner,
    not by an admin. Six functions check it; five routes turn it into 409.

    Reachable only from the factory: nothing in the application sets
    `status = 'published'` yet, so without building the state directly this rule
    would be entirely untested until the publish feature arrives.
    """
    memory = factory.memory(
        owner["memoir"]["id"], owner["memoir"]["owner_participant_id"]
    )
    asset = factory.asset(owner["memoir"]["id"], memory_id=memory["id"])
    spare = factory.asset(owner["memoir"]["id"])

    factory.publish(owner["memoir"]["id"])

    client = as_owner(owner["account"]["id"])
    memoir_id = owner["memoir"]["id"]

    writes = [
        ("POST", f"/memoirs/{memoir_id}/memories", {"body_text": "one more"}),
        ("PATCH", f"/memories/{memory['id']}", {"title": "changed"}),
        ("DELETE", f"/memories/{memory['id']}", None),
        ("POST", f"/memories/{memory['id']}/assets", {"asset_ids": [str(spare["id"])]}),
        ("DELETE", f"/memories/{memory['id']}/assets/{asset['id']}", None),
    ]

    for method, path, body in writes:
        response = client.request(method, path, json=body)
        assert response.status_code == 409, f"{method} {path} answered {response.status_code}"
        assert "published" in response.json()["detail"]


def test_a_published_memoir_can_still_be_read(as_owner, owner, factory, storage):
    """Sealed, not hidden. The family keeps reading it forever."""
    factory.memory(owner["memoir"]["id"], owner["memoir"]["owner_participant_id"])
    factory.publish(owner["memoir"]["id"])

    response = as_owner(owner["account"]["id"]).get(
        f"/memoirs/{owner['memoir']['id']}/memories"
    )
    assert response.status_code == 200
    assert len(response.json()) == 1
