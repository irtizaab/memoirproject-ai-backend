"""Fields the SQL selects and the response must never carry.

Four things in this API are read from the database and deliberately dropped
before the response goes out. Each is a two-layer guarantee — the query omits it
*and* the Pydantic model has no such field — and each is tested at the layer
that actually reaches a browser, because that is the one that matters.

`find_key` and `find_value` search the whole nested body. A top-level assertion
would miss a credential surfacing three levels down inside an asset.
"""

import pytest

from tests.conftest import find_key, find_value, requires_db

pytestmark = [pytest.mark.security, requires_db, pytest.mark.db]


# ---------------------------------------------------------------------------
# contributor_token — the owner has no honest use for it
# ---------------------------------------------------------------------------


def test_the_contributors_list_never_carries_a_contributor_token(
    as_owner, owner, factory
):
    """A credential belonging to somebody else.

    The owner can already see who contributed and delete what they left. The
    only thing the token adds is the ability to post *as* them — impersonation
    inside their own family's memoir. So it is not selected, and the model has
    no field for it either.
    """
    contributor = factory.contributor(owner["memoir"]["id"], display_name="Ali")

    response = as_owner(owner["account"]["id"]).get(
        f"/memoirs/{owner['memoir']['id']}/contributors"
    )
    body = response.json()

    assert response.status_code == 200
    assert not find_key(body, "contributor_token")
    assert not find_value(body, contributor["contributor_token"])


def test_no_response_anywhere_leaks_another_contributors_token(
    client, as_owner, owner, factory, storage
):
    """Swept across every route an owner or contributor can reach."""
    first = factory.contributor(owner["memoir"]["id"], display_name="Ali")
    second = factory.contributor(owner["memoir"]["id"], display_name="Fatima")
    factory.memory(owner["memoir"]["id"], first["id"])
    factory.memory(owner["memoir"]["id"], second["id"])

    memoir_id = owner["memoir"]["id"]
    link = owner["memoir"]["link_token"]
    signed = as_owner(owner["account"]["id"])

    responses = [
        signed.get("/me"),
        signed.get("/billing"),
        signed.get(f"/memoirs/{memoir_id}/memories"),
        signed.get(f"/memoirs/{memoir_id}/contributors"),
        client.get(f"/j/{link}"),
        client.get(
            f"/j/{link}/memories",
            headers={"X-Participant-Token": first["contributor_token"]},
        ),
    ]

    for response in responses:
        assert not find_value(response.json(), second["contributor_token"]), (
            f"{response.request.method} {response.request.url.path} leaked a token"
        )


# ---------------------------------------------------------------------------
# never_forget — the owner's private answer
# ---------------------------------------------------------------------------


def test_the_invitation_never_carries_never_forget(client, factory):
    """"What should we never forget about them" is not a caption.

    It is the owner's private answer, written during onboarding. Anyone who
    forwards the invite link would be forwarding that too — and the link is
    designed to be forwarded, which is exactly why this one field is left out of
    the narrowest model in the codebase.
    """
    account = factory.account()
    memoir = factory.memoir(
        account["id"], never_forget="She never once raised her voice."
    )

    response = client.get(f"/j/{memoir['link_token']}")
    body = response.json()

    assert response.status_code == 200
    assert not find_key(body, "never_forget")
    assert "raised her voice" not in response.text


def test_the_owner_can_still_read_never_forget(as_owner, factory):
    """The control: it is private, not deleted."""
    account = factory.account()
    factory.memoir(account["id"], never_forget="She never once raised her voice.")

    response = as_owner(account["id"]).get("/me")

    assert response.status_code == 200
    assert find_value(response.json(), "She never once raised her voice.")


# ---------------------------------------------------------------------------
# storage_path — the shape of the bucket
# ---------------------------------------------------------------------------


def test_no_response_reveals_a_storage_path(as_owner, owner, factory, storage):
    """Object keys stay server-side.

    They are selected constantly — signing a URL needs one, deleting a file
    needs one — and popped before the model sees them. Leaking them would hand
    out the internal layout of a private bucket, and the paths are the only
    thing standing between a leaked service key and knowing where to look.

    The one place a path legitimately appears is inside the signed download URL
    itself, because that is what the signature is over — Supabase signs
    `/object/sign/<bucket>/<path>`. So the guarantee is narrower than "the
    string never appears": no `storage_path` *field* is ever returned, and any
    occurrence of the path is inside a signed URL that expires. Asserting the
    broader thing would be asserting something signed URLs cannot satisfy.
    """
    memory = factory.memory(
        owner["memoir"]["id"], owner["memoir"]["owner_participant_id"]
    )
    asset = factory.asset(owner["memoir"]["id"], memory_id=memory["id"])

    response = as_owner(owner["account"]["id"]).get(
        f"/memoirs/{owner['memoir']['id']}/memories"
    )

    assert response.status_code == 200
    assert not find_key(response.json(), "storage_path")

    leaked_outside_a_url = [
        value
        for value in _strings(response.json())
        if asset["storage_path"] in value and not value.startswith("http")
    ]
    assert leaked_outside_a_url == []


def _strings(node):
    """Every string anywhere in a decoded JSON body."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


# ---------------------------------------------------------------------------
# transcript internals
# ---------------------------------------------------------------------------


def test_a_transcript_never_carries_its_provider_id_or_error(
    as_owner, owner, factory, storage
):
    """`provider_id` is the handle the webhook is addressed by.

    Anyone holding it plus the shared secret could write to that transcript. It
    is selected because `reconcile` needs it, and dropped twice on the way out —
    once in `to_payload`, again by the model having no such field.

    `error` is dropped for a different reason: a family cannot act on a
    provider's error string, and showing them one would be noise at the worst
    possible moment.
    """
    memory = factory.memory(
        owner["memoir"]["id"], owner["memoir"]["owner_participant_id"], kind="voice"
    )
    asset = factory.asset(
        owner["memoir"]["id"], memory_id=memory["id"], kind="audio",
        mime_type="audio/webm", duration_ms=42_000,
    )
    transcript = factory.transcript(
        asset["id"],
        status="failed",
        text=None,
        provider_id="assemblyai-job-abc123",
        error="upstream said: quota exceeded for account 99",
    )

    response = as_owner(owner["account"]["id"]).get(
        f"/memoirs/{owner['memoir']['id']}/memories"
    )
    body = response.json()

    assert response.status_code == 200
    assert not find_key(body, "provider_id")
    assert not find_key(body, "error")
    assert transcript["provider_id"] not in response.text
    assert "quota exceeded" not in response.text


def test_a_contributor_sees_no_transcript_internals_either(
    client, owner, factory, storage
):
    """The same model serves both audiences, so both get checked."""
    contributor = factory.contributor(owner["memoir"]["id"])
    memory = factory.memory(owner["memoir"]["id"], contributor["id"], kind="voice")
    asset = factory.asset(
        owner["memoir"]["id"], memory_id=memory["id"], kind="audio",
        mime_type="audio/webm", duration_ms=1000,
    )
    factory.transcript(asset["id"], provider_id="job-secret-xyz")

    response = client.get(
        f"/j/{owner['memoir']['link_token']}/memories",
        headers={"X-Participant-Token": contributor["contributor_token"]},
    )

    assert not find_key(response.json(), "provider_id")
    assert "job-secret-xyz" not in response.text


# ---------------------------------------------------------------------------
# The models themselves
# ---------------------------------------------------------------------------


def test_the_response_models_have_no_field_for_any_of_it():
    """The second half of each guarantee.

    A field absent from the model cannot reach a client no matter what the SQL
    selected — `response_model` filters it out. Asserting on the models means a
    query that starts selecting one of these by accident still cannot leak it.
    """
    from src.models.account_models import Contributor
    from src.models.memoir_models import LinkInvitation
    from src.models.memory_models import MediaAsset, Transcript

    assert "contributor_token" not in Contributor.model_fields
    assert "never_forget" not in LinkInvitation.model_fields
    assert "storage_path" not in MediaAsset.model_fields
    assert "provider_id" not in Transcript.model_fields
    assert "error" not in Transcript.model_fields


def test_a_contributor_only_ever_sees_their_own_participant_id(
    client, owner, factory, storage
):
    """`participant_id` and `is_owner` reach the contributor too.

    Deliberate, and defensible: the id is their own row, which they already hold
    a token for, and `is_owner` is false for everything they can see. Asserted
    rather than assumed, because it is the kind of field that gets added to a
    shared model without anyone rechecking who reads it.
    """
    contributor = factory.contributor(owner["memoir"]["id"])
    factory.memory(owner["memoir"]["id"], contributor["id"])

    response = client.get(
        f"/j/{owner['memoir']['link_token']}/memories",
        headers={"X-Participant-Token": contributor["contributor_token"]},
    )

    for memory in response.json():
        assert memory["participant_id"] == str(contributor["id"])
        assert memory["is_owner"] is False
