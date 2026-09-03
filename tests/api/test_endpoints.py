"""Every endpoint, at least once.

The security tier covers the boundaries. This covers the ordinary paths — that
each route does the thing it says, and answers the codes it documents. Ported
from `verify_slice3.py`, which needed a live server, live Supabase and the real
production database to assert the same things.
"""

import uuid

import pytest

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.db]


# ---------------------------------------------------------------------------
# Health and the public price list
# ---------------------------------------------------------------------------


def test_health_reports_the_database(client):
    """The cheapest possible proof the connection string works."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": 1}


def test_the_price_list_is_public(client):
    """Deliberately unauthenticated.

    Onboarding's pricing screen reads it, and that screen sits before signup —
    so requiring a token would mean either moving the screen or hardcoding a
    price. The second is what used to happen: onboarding said $3/month while
    the database said $8, and nothing made them agree.
    """
    response = client.get("/plans")
    assert response.status_code == 200

    plans = response.json()
    assert len(plans) >= 1
    codes = {p["code"] for p in plans}
    assert "keepsake" in codes


# ---------------------------------------------------------------------------
# Drafts, and the claim that turns one into a memoir
# ---------------------------------------------------------------------------


def test_a_draft_is_created_without_any_credential(client):
    """Onboarding starts before anybody has an account.

    The token that comes back is the entire credential for editing it.
    """
    response = client.post("/drafts")
    assert response.status_code == 200
    assert set(response.json()) == {"id", "token"}


def test_a_draft_is_edited_with_its_token(client):
    draft = client.post("/drafts").json()

    response = client.patch(
        f"/drafts/{draft['id']}",
        headers={"X-Draft-Token": draft["token"]},
        json={"subject_name": "Nusrat Bibi", "relationship": "grandchild"},
    )

    assert response.status_code == 200
    assert response.json()["subject_name"] == "Nusrat Bibi"


def test_the_wrong_draft_token_is_404(client):
    draft = client.post("/drafts").json()

    response = client.patch(
        f"/drafts/{draft['id']}",
        headers={"X-Draft-Token": "0" * 48},
        json={"subject_name": "Taken"},
    )
    assert response.status_code == 404


def test_a_missing_draft_token_is_422(client):
    """A required header, so FastAPI refuses before the handler runs."""
    draft = client.post("/drafts").json()
    response = client.patch(f"/drafts/{draft['id']}", json={"subject_name": "x"})
    assert response.status_code == 422


def test_an_empty_patch_is_400(client):
    draft = client.post("/drafts").json()
    response = client.patch(
        f"/drafts/{draft['id']}",
        headers={"X-Draft-Token": draft["token"]},
        json={},
    )
    assert response.status_code == 400


def test_a_living_subject_cannot_have_a_death_year(client):
    """A product rule living in the schema, surfacing as a clean 400.

    `draft_living_has_no_end_year`. The handler maps SQLSTATE 23514 to a 400
    naming the constraint, so the frontend can point at the field rather than
    showing a stack trace.
    """
    draft = client.post("/drafts").json()

    response = client.patch(
        f"/drafts/{draft['id']}",
        headers={"X-Draft-Token": draft["token"]},
        json={"subject_is_living": True, "through_year": 2021},
    )

    assert response.status_code == 400
    assert response.json()["constraint"] == "draft_living_has_no_end_year"


def test_years_must_be_in_order(client):
    draft = client.post("/drafts").json()

    response = client.patch(
        f"/drafts/{draft['id']}",
        headers={"X-Draft-Token": draft["token"]},
        json={"born_year": 2020, "through_year": 1990},
    )

    assert response.status_code == 400
    assert response.json()["constraint"] == "draft_years_ordered"


def test_an_unknown_relationship_is_refused_at_the_edge(client):
    """A `Literal`, so this is a 422 with a field name rather than a database error."""
    draft = client.post("/drafts").json()

    response = client.patch(
        f"/drafts/{draft['id']}",
        headers={"X-Draft-Token": draft["token"]},
        json={"relationship": "cousin"},
    )
    assert response.status_code == 422


class TestClaim:
    """The hinge of onboarding.

    Everything before it is anonymous and disposable; everything after belongs
    to somebody. Four rows in one transaction.
    """

    def _draft(self, client, subject_name="Nusrat Bibi"):
        draft = client.post("/drafts").json()
        if subject_name is not None:
            client.patch(
                f"/drafts/{draft['id']}",
                headers={"X-Draft-Token": draft["token"]},
                json={"subject_name": subject_name},
            )
        return draft

    def test_it_creates_the_account_memoir_participant_and_link(
        self, client, as_owner, db_conn
    ):
        draft = self._draft(client)
        user_id = str(uuid.uuid4())

        with db_conn.cursor() as cur:
            cur.execute("INSERT INTO auth.users (id) VALUES (%(id)s)", {"id": user_id})
        db_conn.commit()

        response = as_owner(user_id).post(
            "/memoirs/claim",
            headers={"X-Draft-Token": draft["token"]},
            json={"draft_id": draft["id"]},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["subject_name"] == "Nusrat Bibi"
        assert body["link_token"]

        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS n FROM memoir_participant
                 WHERE memoir_id = %(m)s AND role = 'owner'
                """,
                {"m": body["id"]},
            )
            assert cur.fetchone()["n"] == 1, "the owner participant was not created"

    def test_a_draft_with_no_subject_name_is_400(self, client, as_owner, db_conn):
        draft = self._draft(client, subject_name=None)
        user_id = str(uuid.uuid4())
        with db_conn.cursor() as cur:
            cur.execute("INSERT INTO auth.users (id) VALUES (%(id)s)", {"id": user_id})
        db_conn.commit()

        response = as_owner(user_id).post(
            "/memoirs/claim",
            headers={"X-Draft-Token": draft["token"]},
            json={"draft_id": draft["id"]},
        )
        assert response.status_code == 400

    def test_claiming_twice_is_409(self, client, as_owner, owner):
        """The double-click guard, and the one-memoir rule.

        The account already owns a memoir, so the answer is 409 whatever is
        wrong with the draft — the guard runs before the draft is even read.
        """
        draft = self._draft(client)

        response = as_owner(owner["account"]["id"]).post(
            "/memoirs/claim",
            headers={"X-Draft-Token": draft["token"]},
            json={"draft_id": draft["id"]},
        )
        assert response.status_code == 409

    def test_claim_needs_both_credentials(self, client, owner):
        """A bearer token and a draft token. Missing either one refuses."""
        draft = self._draft(client)

        no_bearer = client.post(
            "/memoirs/claim",
            headers={"X-Draft-Token": draft["token"]},
            json={"draft_id": draft["id"]},
        )
        assert no_bearer.status_code == 401


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------


def test_me_returns_an_empty_list_for_a_new_account(as_owner):
    """A normal state, not a 404.

    Somebody who signed up and claimed nothing has an identity and no memoir.
    """
    response = as_owner(str(uuid.uuid4())).get("/me")
    assert response.status_code == 200
    assert response.json()["memoirs"] == []


def test_me_carries_the_memoir_and_its_link(as_owner, owner):
    response = as_owner(owner["account"]["id"]).get("/me")

    assert response.status_code == 200
    memoirs = response.json()["memoirs"]
    assert len(memoirs) == 1
    assert memoirs[0]["link_token"] == owner["memoir"]["link_token"]


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------


class TestMemories:
    def test_creating_and_listing(self, as_owner, owner, storage):
        client = as_owner(owner["account"]["id"])
        memoir_id = owner["memoir"]["id"]

        created = client.post(
            f"/memoirs/{memoir_id}/memories",
            json={"title": "The kitchen", "body_text": "She always sang."},
        )
        assert created.status_code == 201
        assert created.json()["kind"] == "text"
        assert created.json()["is_owner"] is True

        listing = client.get(f"/memoirs/{memoir_id}/memories")
        assert listing.status_code == 200
        assert [m["title"] for m in listing.json()] == ["The kitchen"]

    def test_an_empty_memory_is_refused(self, as_owner, owner, storage):
        """A memory needs something in it — words, a photograph, or a recording."""
        response = as_owner(owner["account"]["id"]).post(
            f"/memoirs/{owner['memoir']['id']}/memories", json={"title": "Nothing"}
        )
        assert response.status_code == 400

    def test_kind_is_derived_from_the_contents(self, as_owner, owner, factory, storage):
        """Any audio makes it a voice note; else any image makes it a photo.

        Never sent by the client, so the label and the contents cannot disagree.
        """
        client = as_owner(owner["account"]["id"])
        memoir_id = owner["memoir"]["id"]

        photo = factory.asset(memoir_id, kind="image")
        recording = factory.asset(
            memoir_id, kind="audio", mime_type="audio/webm", duration_ms=4000
        )

        photo_memory = client.post(
            f"/memoirs/{memoir_id}/memories", json={"asset_ids": [str(photo["id"])]}
        )
        assert photo_memory.json()["kind"] == "photo"

        mixed = client.post(
            f"/memoirs/{memoir_id}/memories",
            json={
                "body_text": "and a note",
                "asset_ids": [str(recording["id"])],
            },
        )
        assert mixed.json()["kind"] == "voice", "audio should win over text"

    def test_editing_leaves_unmentioned_fields_alone(self, as_owner, owner, factory, storage):
        memory = factory.memory(
            owner["memoir"]["id"],
            owner["memoir"]["owner_participant_id"],
            title="Before",
            body_text="Kept.",
        )

        response = as_owner(owner["account"]["id"]).patch(
            f"/memories/{memory['id']}", json={"title": "After"}
        )

        assert response.status_code == 200
        assert response.json()["title"] == "After"
        assert response.json()["body_text"] == "Kept."

    def test_emptying_a_text_memory_is_refused_and_rolls_back(
        self, as_owner, owner, factory, storage
    ):
        """400, and the words are still there afterwards.

        The refusal happens inside the transaction, so the update that caused it
        is undone. A rejection that had already destroyed the text would be the
        worst of both.
        """
        memory = factory.memory(
            owner["memoir"]["id"],
            owner["memoir"]["owner_participant_id"],
            body_text="Still here.",
        )
        client = as_owner(owner["account"]["id"])

        refused = client.patch(f"/memories/{memory['id']}", json={"body_text": ""})
        assert refused.status_code == 400

        after = client.get(f"/memories/{memory['id']}")
        assert after.json()["body_text"] == "Still here."

    def test_deleting_removes_the_memory_and_its_files(
        self, as_owner, owner, factory, storage
    ):
        memory = factory.memory(
            owner["memoir"]["id"], owner["memoir"]["owner_participant_id"]
        )
        asset = factory.asset(owner["memoir"]["id"], memory_id=memory["id"])
        storage.put(asset["storage_path"])

        client = as_owner(owner["account"]["id"])
        assert client.delete(f"/memories/{memory['id']}").status_code == 204

        assert client.get(f"/memories/{memory['id']}").status_code == 404
        assert asset["storage_path"] in storage.deleted, (
            "the row went but the file stayed"
        )

    def test_an_empty_patch_body_is_400(self, as_owner, owner, factory, storage):
        memory = factory.memory(
            owner["memoir"]["id"], owner["memoir"]["owner_participant_id"]
        )
        response = as_owner(owner["account"]["id"]).patch(
            f"/memories/{memory['id']}", json={}
        )
        assert response.status_code == 400


class TestMemoryAssets:
    """Adding and removing media after the fact."""

    def test_attaching_re_derives_the_kind(self, as_owner, owner, factory, storage):
        memory = factory.memory(
            owner["memoir"]["id"], owner["memoir"]["owner_participant_id"]
        )
        recording = factory.asset(
            owner["memoir"]["id"], kind="audio", mime_type="audio/webm", duration_ms=1000
        )

        response = as_owner(owner["account"]["id"]).post(
            f"/memories/{memory['id']}/assets",
            json={"asset_ids": [str(recording["id"])]},
        )

        assert response.status_code == 200
        assert response.json()["kind"] == "voice"
        assert len(response.json()["assets"]) == 1

    def test_removing_deletes_the_file_and_re_derives(
        self, as_owner, owner, factory, storage
    ):
        memory = factory.memory(
            owner["memoir"]["id"],
            owner["memoir"]["owner_participant_id"],
            kind="photo",
            body_text="Words as well.",
        )
        photo = factory.asset(owner["memoir"]["id"], memory_id=memory["id"])
        storage.put(photo["storage_path"])

        response = as_owner(owner["account"]["id"]).delete(
            f"/memories/{memory['id']}/assets/{photo['id']}"
        )

        assert response.status_code == 200
        assert response.json()["kind"] == "text", "kind still claims a photograph"
        assert photo["storage_path"] in storage.deleted

    def test_removing_the_last_thing_is_refused_and_keeps_the_file(
        self, as_owner, owner, factory, storage
    ):
        """400, and nothing is deleted.

        The refusal happens before the transaction commits, so the row is still
        attached — and because the object is only deleted afterwards, the file
        is still in storage too.
        """
        memory = factory.memory(
            owner["memoir"]["id"],
            owner["memoir"]["owner_participant_id"],
            kind="photo",
            body_text=None,
        )
        photo = factory.asset(owner["memoir"]["id"], memory_id=memory["id"])
        storage.put(photo["storage_path"])

        client = as_owner(owner["account"]["id"])
        response = client.delete(f"/memories/{memory['id']}/assets/{photo['id']}")

        assert response.status_code == 400
        assert storage.deleted == [], "a file was deleted by a rejected request"
        assert len(client.get(f"/memories/{memory['id']}").json()["assets"]) == 1

    def test_attaching_nothing_is_422(self, as_owner, owner, factory, storage):
        memory = factory.memory(
            owner["memoir"]["id"], owner["memoir"]["owner_participant_id"]
        )
        response = as_owner(owner["account"]["id"]).post(
            f"/memories/{memory['id']}/assets", json={"asset_ids": []}
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


class TestUploads:
    def test_the_three_step_dance(self, as_owner, owner, storage):
        """Reserve, PUT, confirm — and the size comes from storage.

        The client never says how big the file is. A storage meter is only
        meaningful if the number behind it is one the uploader cannot choose.
        """
        client = as_owner(owner["account"]["id"])

        ticket = client.post(
            "/media/uploads",
            json={
                "memoir_id": str(owner["memoir"]["id"]),
                "kind": "image",
                "mime_type": "image/jpeg",
            },
        )
        assert ticket.status_code == 201
        assert ticket.json()["upload_url"]

        path = storage.signed_uploads[-1]
        storage.put(path, b"x" * 4096)

        confirmed = client.post(f"/media/uploads/{ticket.json()['asset_id']}/complete")
        assert confirmed.status_code == 200
        assert confirmed.json()["byte_size"] == 4096

    def test_confirming_without_an_uploaded_file_is_409(self, as_owner, owner, storage):
        """The PUT never landed. The honest answer is "nothing arrived"."""
        client = as_owner(owner["account"]["id"])

        ticket = client.post(
            "/media/uploads",
            json={
                "memoir_id": str(owner["memoir"]["id"]),
                "kind": "image",
                "mime_type": "image/jpeg",
            },
        ).json()

        response = client.post(f"/media/uploads/{ticket['asset_id']}/complete")
        assert response.status_code == 409

    def test_a_contributor_can_upload_with_a_link_token(self, client, owner, storage):
        """Both credentials reach the same place.

        A contributor has no bearer token, and the endpoint does not prefer one
        — it takes whatever arrived and asks whether it is enough.
        """
        response = client.post(
            "/media/uploads",
            headers={"X-Link-Token": owner["memoir"]["link_token"]},
            json={
                "memoir_id": str(owner["memoir"]["id"]),
                "kind": "audio",
                "mime_type": "audio/webm",
                "duration_ms": 4200,
            },
        )
        assert response.status_code == 201

    def test_no_credential_at_all_is_401(self, client, owner):
        response = client.post(
            "/media/uploads",
            json={
                "memoir_id": str(owner["memoir"]["id"]),
                "kind": "image",
                "mime_type": "image/jpeg",
            },
        )
        assert response.status_code == 401

    def test_a_link_token_for_another_memoir_is_404(
        self, client, owner, stranger, storage
    ):
        """A live link to memoir A must not authorize an upload into memoir B."""
        response = client.post(
            "/media/uploads",
            headers={"X-Link-Token": owner["memoir"]["link_token"]},
            json={
                "memoir_id": str(stranger["memoir"]["id"]),
                "kind": "image",
                "mime_type": "image/jpeg",
            },
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Contributing through the link
# ---------------------------------------------------------------------------


class TestContributing:
    def test_a_stranger_leaves_a_memory_and_is_issued_a_token(
        self, client, owner, storage
    ):
        response = client.post(
            f"/j/{owner['memoir']['link_token']}/memories",
            json={"display_name": "Ali", "body_text": "I remember the garden."},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["memory"]["contributor_name"] == "Ali"
        assert body["memory"]["is_owner"] is False
        assert body["participant_token"]

    def test_a_returning_contributor_stays_one_person(self, client, owner, storage):
        link = owner["memoir"]["link_token"]

        first = client.post(
            f"/j/{link}/memories",
            json={"display_name": "Ali", "body_text": "One."},
        ).json()

        second = client.post(
            f"/j/{link}/memories",
            json={
                "display_name": "Ali",
                "body_text": "Two.",
                "participant_token": first["participant_token"],
            },
        ).json()

        assert second["participant_token"] == first["participant_token"]
        assert (
            second["memory"]["participant_id"] == first["memory"]["participant_id"]
        )

    def test_a_new_name_renames_their_earlier_memories(self, client, owner, storage):
        """The name lives on the person, not on each memory.

        So changing it changes what is shown above everything they have already
        sent. That is the honest reading of "this is my name", and the
        contributor screen says so before they change it.
        """
        link = owner["memoir"]["link_token"]

        first = client.post(
            f"/j/{link}/memories", json={"display_name": "Ali", "body_text": "One."}
        ).json()
        token = first["participant_token"]

        client.post(
            f"/j/{link}/memories",
            json={
                "display_name": "Ali Raza",
                "body_text": "Two.",
                "participant_token": token,
            },
        )

        mine = client.get(
            f"/j/{link}/memories", headers={"X-Participant-Token": token}
        ).json()

        assert {m["contributor_name"] for m in mine} == {"Ali Raza"}

    def test_a_blank_name_leaves_the_stored_one_alone(self, client, owner, storage):
        """Whitespace passes Pydantic's `min_length=1`.

        Writing it would violate `participant_name_not_blank` and cost them the
        contribution over a stray space, so the stored name is kept instead.
        """
        link = owner["memoir"]["link_token"]

        first = client.post(
            f"/j/{link}/memories", json={"display_name": "Ali", "body_text": "One."}
        ).json()

        second = client.post(
            f"/j/{link}/memories",
            json={
                "display_name": "   ",
                "body_text": "Two.",
                "participant_token": first["participant_token"],
            },
        )

        assert second.status_code == 201
        assert second.json()["memory"]["contributor_name"] == "Ali"

    def test_a_second_device_becomes_a_second_person(self, client, owner, storage):
        """Identity is the token, and a second browser holds none.

        Not a bug to fix silently — two people genuinely share names, so the
        merge below is the owner's decision rather than the system's guess.
        """
        link = owner["memoir"]["link_token"]

        phone = client.post(
            f"/j/{link}/memories", json={"display_name": "Ali", "body_text": "Phone."}
        ).json()
        laptop = client.post(
            f"/j/{link}/memories", json={"display_name": "Ali", "body_text": "Laptop."}
        ).json()

        assert phone["participant_token"] != laptop["participant_token"]
        assert (
            phone["memory"]["participant_id"] != laptop["memory"]["participant_id"]
        )

    def test_an_empty_contribution_is_400(self, client, owner, storage):
        response = client.post(
            f"/j/{owner['memoir']['link_token']}/memories",
            json={"display_name": "Ali"},
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Contributors, and merging
# ---------------------------------------------------------------------------


class TestContributors:
    def test_the_list_counts_memories_and_shows_the_link(
        self, as_owner, owner, factory, storage
    ):
        contributor = factory.contributor(owner["memoir"]["id"], display_name="Ali")
        factory.memory(owner["memoir"]["id"], contributor["id"])
        factory.memory(owner["memoir"]["id"], contributor["id"])

        response = as_owner(owner["account"]["id"]).get(
            f"/memoirs/{owner['memoir']['id']}/contributors"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["link"]["token"] == owner["memoir"]["link_token"]

        ali = next(p for p in body["participants"] if p["display_name"] == "Ali")
        assert ali["memory_count"] == 2

    def test_somebody_who_opened_and_wrote_nothing_still_appears(
        self, as_owner, owner, factory
    ):
        """The middle state, and the reason this screen exists.

        A LEFT JOIN rather than an inner one — a person to ring, not a row to
        hide.
        """
        factory.contributor(owner["memoir"]["id"], display_name="Quiet One")

        response = as_owner(owner["account"]["id"]).get(
            f"/memoirs/{owner['memoir']['id']}/contributors"
        )

        quiet = next(
            p for p in response.json()["participants"] if p["display_name"] == "Quiet One"
        )
        assert quiet["memory_count"] == 0
        assert quiet["first_opened_at"] is not None

    def test_merging_moves_the_memories_and_leaves_one_entry(
        self, as_owner, owner, factory, storage
    ):
        keep = factory.contributor(owner["memoir"]["id"], display_name="Ali")
        spare = factory.contributor(owner["memoir"]["id"], display_name="Ali")
        factory.memory(owner["memoir"]["id"], keep["id"])
        factory.memory(owner["memoir"]["id"], spare["id"])

        client = as_owner(owner["account"]["id"])
        memoir_id = owner["memoir"]["id"]

        merged = client.post(
            f"/memoirs/{memoir_id}/contributors/{spare['id']}/merge-into/{keep['id']}"
        )

        assert merged.status_code == 200
        assert merged.json()["memories_moved"] == 1

        listing = client.get(f"/memoirs/{memoir_id}/contributors").json()
        alis = [p for p in listing["participants"] if p["display_name"] == "Ali"]
        assert len(alis) == 1
        assert alis[0]["memory_count"] == 2

    def test_the_merged_device_still_works_and_does_not_fork_again(
        self, client, as_owner, owner, storage
    ):
        """Why the losing row is kept rather than deleted.

        That device still holds its token. Delete the row and the token resolves
        to nothing, so the next contribution creates a *third* participant — the
        original bug, back again, silently.
        """
        link = owner["memoir"]["link_token"]

        phone = client.post(
            f"/j/{link}/memories", json={"display_name": "Ali", "body_text": "Phone."}
        ).json()
        laptop = client.post(
            f"/j/{link}/memories", json={"display_name": "Ali", "body_text": "Laptop."}
        ).json()

        owner_client = as_owner(owner["account"]["id"])
        memoir_id = owner["memoir"]["id"]

        owner_client.post(
            f"/memoirs/{memoir_id}/contributors/"
            f"{laptop['memory']['participant_id']}/merge-into/"
            f"{phone['memory']['participant_id']}"
        )

        again = client.post(
            f"/j/{link}/memories",
            json={
                "display_name": "Ali",
                "body_text": "Laptop again.",
                "participant_token": laptop["participant_token"],
            },
        )

        assert again.status_code == 201
        assert (
            again.json()["memory"]["participant_id"]
            == phone["memory"]["participant_id"]
        ), "the merged device forked into a third person"

        listing = owner_client.get(f"/memoirs/{memoir_id}/contributors").json()
        alis = [p for p in listing["participants"] if p["display_name"] == "Ali"]
        assert len(alis) == 1
        assert alis[0]["memory_count"] == 3


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------


class TestBilling:
    def test_it_reports_real_storage_against_the_plan(self, as_owner, owner, factory):
        """Summed from confirmed uploads, never counted.

        A running total has to survive a failed upload, a deleted memory and a
        duplicate webhook — and eventually it does not, silently, with no way to
        tell when it started lying.
        """
        factory.asset(owner["memoir"]["id"], byte_size=2048, uploaded=True)
        factory.asset(owner["memoir"]["id"], byte_size=9999, uploaded=False)

        response = as_owner(owner["account"]["id"]).get("/billing")

        assert response.status_code == 200
        body = response.json()
        assert body["storage"]["used_bytes"] == 2048, (
            "an unconfirmed reservation was charged to somebody"
        )
        assert body["payments_enabled"] is False

    def test_choosing_a_term_records_it_without_charging(self, as_owner, owner):
        """An entitlement change, not a charge.

        It exists so the billing screen quotes the term chosen on the pricing
        screen. Stripe is not wired up, and the response says so plainly.
        """
        response = as_owner(owner["account"]["id"]).patch(
            "/billing/plan", json={"code": "keepsake_yearly"}
        )

        assert response.status_code == 200
        assert response.json()["plan"]["code"] == "keepsake_yearly"
        assert response.json()["payments_enabled"] is False

    def test_an_unknown_plan_is_404(self, as_owner, owner):
        response = as_owner(owner["account"]["id"]).patch(
            "/billing/plan", json={"code": "enterprise-unlimited"}
        )
        assert response.status_code == 404
