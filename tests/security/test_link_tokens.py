"""The credential a contributor holds, and the one they are issued.

Contributors never make an account. The token in the link is the entire
credential for writing to somebody's memoir, and the participant token they get
back is the only credential this API ever issues.

Both are 24 random bytes as 48 hex characters, from `gen_random_bytes`. That
entropy is the whole defence — there is no rate limiting anywhere in this
codebase, which is fine at 192 bits and worth knowing.
"""

import uuid

import pytest

from tests.conftest import TOKEN_PATTERN, requires_db

pytestmark = [pytest.mark.security, requires_db, pytest.mark.db]


# ---------------------------------------------------------------------------
# Four conditions, one answer
# ---------------------------------------------------------------------------


def test_an_unknown_link_is_404(client):
    assert client.get(f"/j/{uuid.uuid4().hex}").status_code == 404


def test_every_way_a_link_can_be_unusable_looks_the_same(client, owner, factory):
    """`contributable_memoir` checks four things in one query.

    The token exists, the link has not been revoked, its scope is `contribute`
    rather than `view`, and the memoir is still a draft. Each failure produces
    one identical 404.

    A contributor cannot act on the difference, and spelling it out would tell
    somebody holding a dead link exactly why it died — which is the beginning of
    working out what else is there.
    """
    contribution = {"display_name": "Ali", "body_text": "I remember the garden."}

    # 1. A token that was never issued.
    unknown = client.post(f"/j/{uuid.uuid4().hex}/memories", json=contribution)

    # 2. A revoked link.
    revoked_memoir = owner["memoir"]
    factory.revoke_link(revoked_memoir["link_id"])
    revoked = client.post(
        f"/j/{revoked_memoir['link_token']}/memories", json=contribution
    )

    # 3. A link that only grants viewing.
    view_only = factory.link(revoked_memoir["id"], scope="view")
    viewing = client.post(f"/j/{view_only['token']}/memories", json=contribution)

    # 4. A memoir that has been published.
    published_owner = factory.account()
    published = factory.memoir(published_owner["id"])
    factory.publish(published["id"])
    sealed = client.post(f"/j/{published['link_token']}/memories", json=contribution)

    answers = {r.status_code for r in [unknown, revoked, viewing, sealed]}
    bodies = {r.text for r in [unknown, revoked, viewing, sealed]}

    assert answers == {404}
    assert len(bodies) == 1, f"four different answers: {bodies}"


def test_a_revoked_link_stops_working_immediately(client, owner, factory):
    """Revoking is the only lever an owner has over contributors.

    It has to bite at once — the reason to revoke is that the link has travelled
    further than intended, and a grace period is the opposite of the point.
    """
    token = owner["memoir"]["link_token"]
    assert client.get(f"/j/{token}").status_code == 200

    factory.revoke_link(owner["memoir"]["link_id"])

    assert client.get(f"/j/{token}").status_code == 404


def test_a_view_scope_link_resolves_but_cannot_contribute(client, owner, factory):
    """The two routes read the scope differently, on purpose.

    `GET /j/{token}` does not filter on scope — it returns the invitation and
    reports what the scope is, so a viewer sees whose memoir it is.
    `POST /j/{token}/memories` requires `contribute`.
    """
    factory.revoke_link(owner["memoir"]["link_id"])
    view_link = factory.link(owner["memoir"]["id"], scope="view")

    invitation = client.get(f"/j/{view_link['token']}")
    assert invitation.status_code == 200
    assert invitation.json()["scope"] == "view"

    attempt = client.post(
        f"/j/{view_link['token']}/memories",
        json={"display_name": "Ali", "body_text": "x"},
    )
    assert attempt.status_code == 404


# ---------------------------------------------------------------------------
# Cross-memoir
# ---------------------------------------------------------------------------


def test_a_participant_token_is_useless_on_another_memoir(client, owner, factory):
    """Tokens are checked together with the memoir they belong to.

    Someone who contributes to two memoirs holds two unrelated tokens, and
    neither works on the other's. Without the `memoir_id` in that WHERE clause,
    one contributor's token would read another family's archive.
    """
    theirs = factory.contributor(owner["memoir"]["id"], display_name="Ali")

    second_owner = factory.account()
    second = factory.memoir(second_owner["id"])

    response = client.get(
        f"/j/{second['link_token']}/memories",
        headers={"X-Participant-Token": theirs["contributor_token"]},
    )

    assert response.status_code == 200
    assert response.json() == [], "a token from another memoir returned rows"


def test_a_contributor_sees_only_their_own(client, owner, factory, storage):
    """The promise the contributor screen makes, kept in a WHERE clause.

    A contributor may see what they wrote. They must never see the archive or
    anyone else's memories — and there is no separate endpoint enforcing that,
    only this filter.
    """
    mine = factory.contributor(owner["memoir"]["id"], display_name="Ali")
    someone_else = factory.contributor(owner["memoir"]["id"], display_name="Fatima")

    factory.memory(owner["memoir"]["id"], mine["id"], body_text="Mine.")
    factory.memory(owner["memoir"]["id"], someone_else["id"], body_text="Not mine.")
    factory.memory(
        owner["memoir"]["id"],
        owner["memoir"]["owner_participant_id"],
        body_text="The owner's.",
    )

    response = client.get(
        f"/j/{owner['memoir']['link_token']}/memories",
        headers={"X-Participant-Token": mine["contributor_token"]},
    )

    assert response.status_code == 200
    bodies = [m["body_text"] for m in response.json()]
    assert bodies == ["Mine."]


def test_a_contributor_never_learns_who_else_is_here(client, owner, factory, storage):
    """The guest list is owner-only.

    Nothing in a contributor's response names another participant.
    """
    other = factory.contributor(owner["memoir"]["id"], display_name="Fatima Bibi")
    factory.memory(owner["memoir"]["id"], other["id"], body_text="Hers.")

    mine = factory.contributor(owner["memoir"]["id"], display_name="Ali")
    factory.memory(owner["memoir"]["id"], mine["id"], body_text="Mine.")

    response = client.get(
        f"/j/{owner['memoir']['link_token']}/memories",
        headers={"X-Participant-Token": mine["contributor_token"]},
    )

    assert "Fatima" not in response.text
    assert "Hers." not in response.text


def test_a_forged_participant_token_reads_nothing(client, owner):
    """A guessed token returns an empty list rather than an error.

    Recorded as current behaviour, and it is a small oracle: an empty 200 tells
    the caller the *link* is live, where a 404 would not. Harmless in practice —
    the link token is already in their hands if they got this far — but worth
    stating rather than discovering.
    """
    response = client.get(
        f"/j/{owner['memoir']['link_token']}/memories",
        headers={"X-Participant-Token": "0" * 48},
    )

    assert response.status_code == 200
    assert response.json() == []


def test_the_participant_token_header_is_required(client, owner):
    """Without it there is no way to know whose contributions to return."""
    response = client.get(f"/j/{owner['memoir']['link_token']}/memories")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# What the tokens look like
# ---------------------------------------------------------------------------


def test_every_issued_token_has_192_bits_of_entropy(client, owner, factory):
    """24 random bytes, hex-encoded, from a CSPRNG.

    There is no rate limiting on any of these routes — no lockout, no CAPTCHA,
    nothing slowing a guess. At 192 bits that is a reasonable trade rather than
    an oversight, but it is a trade, and it rests entirely on this shape.
    """
    draft = client.post("/drafts").json()
    assert TOKEN_PATTERN.match(draft["token"]), draft["token"]

    assert TOKEN_PATTERN.match(owner["memoir"]["link_token"])

    receipt = client.post(
        f"/j/{owner['memoir']['link_token']}/memories",
        json={"display_name": "Ali", "body_text": "Something."},
    ).json()
    assert TOKEN_PATTERN.match(receipt["participant_token"])


def test_reissuing_a_link_produces_a_different_token(as_owner, owner):
    """And kills the old one."""
    old = owner["memoir"]["link_token"]

    response = as_owner(owner["account"]["id"]).post(
        f"/memoirs/{owner['memoir']['id']}/link/reissue"
    )

    assert response.status_code == 201
    new = response.json()["token"]
    assert new != old
    assert TOKEN_PATTERN.match(new)


def test_a_reissued_link_kills_the_old_one_for_contributions(client, as_owner, owner):
    """The remedy for a link that has gone too far.

    Everyone holding the old URL loses access the moment this returns —
    including people who were going to contribute this weekend. Nothing already
    contributed is affected: revoking a link closes the door, it does not empty
    the room.
    """
    old = owner["memoir"]["link_token"]

    as_owner(owner["account"]["id"]).post(
        f"/memoirs/{owner['memoir']['id']}/link/reissue"
    )

    dead = client.post(
        f"/j/{old}/memories", json={"display_name": "Ali", "body_text": "Late."}
    )
    assert dead.status_code == 404
