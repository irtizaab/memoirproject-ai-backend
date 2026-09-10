"""The question library: what the family is asked, and who decides it.

The promises worth testing are not about the model, which is stubbed
throughout. They are the ones that have to hold whichever set is in use:

  - a hand-edited question survives a reprompt
  - switching to the standard set does not destroy the custom one
  - a contributor sees their own group's questions and nobody else's
  - a contributor never sees an empty page, however the feature fails
  - a contributor never learns which questions a model wrote, or what the
    owner typed to produce them
"""

import pytest

from src.domain.prompts import prompt_service
from src.domain.prompts.standard_questions import standard_for
from src.integrations import gemini
from src.models.prompt_models import GroupQuestions, Library
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.db]


GROUPS = ["child", "grandchild", "spouse_partner", "friend", "self", "other"]


def _drafted(marker: str = "drafted") -> Library:
    """A plausible model reply: five questions for every group."""
    return Library(
        groups=[
            GroupQuestions(
                relationship=group,
                questions=[f"{marker} {group} question {n}?" for n in range(5)],
            )
            for group in GROUPS
        ]
    )


@pytest.fixture
def stub_gemini(monkeypatch):
    """Replace the model call. Returns a setter, so a test can choose the reply.

    Patched on `prompt_service`'s own reference rather than on the integration,
    so a test that forgets to stub makes a real HTTP call and fails loudly
    instead of silently reaching Google with whatever key is in `.env`.
    """

    def _set(reply):
        async def _fake(prompt, schema, **kwargs):
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(prompt_service.gemini, "generate", _fake)

    _set(_drafted())
    return _set


# ---------------------------------------------------------------------------
# The owner's side
# ---------------------------------------------------------------------------


def test_a_new_memoir_starts_on_the_standard_questions(as_owner, owner):
    """The default matters: nobody's family should meet a blank page.

    An owner who never opens this screen still has questions, which is why
    `questions_mode` defaults to 'standard' in migration 0014 rather than to
    something meaning "none chosen yet".
    """
    memoir_id = str(owner["memoir"]["id"])

    response = as_owner(str(owner["account"]["id"])).get(
        f"/memoirs/{memoir_id}/questions"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "standard"
    assert body["groups"] == [
        {"relationship": group, "questions": []} for group in GROUPS
    ]
    # And the shipped set comes back too, so the screen can show what the
    # choice actually means rather than describing it.
    assert len(body["standard"]) == len(GROUPS)


def test_generating_writes_a_library_and_switches_the_memoir_onto_it(
    as_owner, owner, stub_gemini
):
    """Drafting is the owner saying they want these used.

    Leaving the memoir on 'standard' after generating would mean the work they
    just watched happen changed nothing a contributor sees.
    """
    memoir_id = str(owner["memoir"]["id"])

    response = as_owner(str(owner["account"]["id"])).post(
        f"/memoirs/{memoir_id}/questions/generate",
        json={"notes": "She kept bees and ran the shop on Depot Street."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "custom"
    assert body["subject_notes"] == "She kept bees and ran the shop on Depot Street."

    for group in body["groups"]:
        assert len(group["questions"]) == 5
        assert all(q["source"] == "ai" for q in group["questions"])


def test_the_notes_survive_a_failed_generation(as_owner, owner, stub_gemini):
    """A model that is unreachable must not also cost the paragraph they typed.

    Saved in the first transaction, before the call. This is the difference
    between "try again" and "type all that again".
    """
    memoir_id = str(owner["memoir"]["id"])
    client = as_owner(str(owner["account"]["id"]))

    stub_gemini(gemini.GeminiError("upstream is down"))
    failed = client.post(
        f"/memoirs/{memoir_id}/questions/generate",
        json={"notes": "Everything I know about her."},
    )

    # 503, not 500: nothing here is broken, an upstream service was away.
    assert failed.status_code == 503

    body = client.get(f"/memoirs/{memoir_id}/questions").json()
    assert body["subject_notes"] == "Everything I know about her."
    # And the memoir is still on the set that works.
    assert body["mode"] == "standard"


def test_the_switch_being_off_is_not_an_error_worth_a_traceback(
    as_owner, owner, stub_gemini
):
    """`AI_ENABLED=false` answers the same 503, with nothing spent."""
    memoir_id = str(owner["memoir"]["id"])
    stub_gemini(gemini.GeminiDisabled("AI_ENABLED is false"))

    response = as_owner(str(owner["account"]["id"])).post(
        f"/memoirs/{memoir_id}/questions/generate", json={"notes": None}
    )

    assert response.status_code == 503


def test_editing_a_question_makes_it_the_owners(as_owner, owner, stub_gemini):
    """`source` flips to 'owner', and nothing else in the system sets it."""
    memoir_id = str(owner["memoir"]["id"])
    client = as_owner(str(owner["account"]["id"]))

    library = client.post(
        f"/memoirs/{memoir_id}/questions/generate", json={"notes": None}
    ).json()
    target = library["groups"][0]["questions"][0]

    response = client.patch(
        f"/questions/{target['id']}",
        json={"body": "What did she keep on the shop counter?"},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "owner"
    assert response.json()["body"] == "What did she keep on the shop counter?"


def test_a_reprompt_keeps_what_you_wrote_by_hand(as_owner, owner, stub_gemini):
    """The whole reason `source` exists.

    Losing a generated question costs a model call. Losing a hand-written one
    costs the sentence a person chose, and they will not remember it.
    """
    memoir_id = str(owner["memoir"]["id"])
    client = as_owner(str(owner["account"]["id"]))

    library = client.post(
        f"/memoirs/{memoir_id}/questions/generate", json={"notes": None}
    ).json()
    target = library["groups"][0]["questions"][0]
    mine = "What did she keep on the shop counter?"
    client.patch(f"/questions/{target['id']}", json={"body": mine})

    stub_gemini(_drafted("second pass"))
    again = client.post(
        f"/memoirs/{memoir_id}/questions/generate", json={"notes": None}
    ).json()

    bodies = [q["body"] for group in again["groups"] for q in group["questions"]]
    assert mine in bodies
    # And the model's first attempt is gone, replaced by its second.
    assert not any(b.startswith("drafted ") for b in bodies)
    assert any(b.startswith("second pass ") for b in bodies)


def test_replace_edited_is_the_owner_saying_start_again(
    as_owner, owner, stub_gemini
):
    """Theirs to say, and never the default."""
    memoir_id = str(owner["memoir"]["id"])
    client = as_owner(str(owner["account"]["id"]))

    library = client.post(
        f"/memoirs/{memoir_id}/questions/generate", json={"notes": None}
    ).json()
    target = library["groups"][0]["questions"][0]
    mine = "What did she keep on the shop counter?"
    client.patch(f"/questions/{target['id']}", json={"body": mine})

    stub_gemini(_drafted("clean slate"))
    again = client.post(
        f"/memoirs/{memoir_id}/questions/generate",
        json={"notes": None, "replace_edited": True},
    ).json()

    bodies = [q["body"] for group in again["groups"] for q in group["questions"]]
    assert mine not in bodies


def test_switching_to_standard_does_not_destroy_the_custom_set(
    as_owner, owner, stub_gemini
):
    """Otherwise the choice is one-way and nobody would risk making it."""
    memoir_id = str(owner["memoir"]["id"])
    client = as_owner(str(owner["account"]["id"]))
    client.post(f"/memoirs/{memoir_id}/questions/generate", json={"notes": None})

    back = client.patch(
        f"/memoirs/{memoir_id}/questions/mode", json={"mode": "standard"}
    ).json()

    assert back["mode"] == "standard"
    assert all(group["questions"] for group in back["groups"])


def test_deleting_a_question_leaves_the_rest_in_order(
    as_owner, owner, stub_gemini
):
    """The gap in `ordinal` is deliberate: order is read, not counted."""
    memoir_id = str(owner["memoir"]["id"])
    client = as_owner(str(owner["account"]["id"]))
    library = client.post(
        f"/memoirs/{memoir_id}/questions/generate", json={"notes": None}
    ).json()

    group = library["groups"][0]
    doomed = group["questions"][1]
    survivors = [q["body"] for q in group["questions"] if q["id"] != doomed["id"]]

    assert client.delete(f"/questions/{doomed['id']}").status_code == 204

    after = client.get(f"/memoirs/{memoir_id}/questions").json()
    kept = next(
        g for g in after["groups"] if g["relationship"] == group["relationship"]
    )
    assert [q["body"] for q in kept["questions"]] == survivors


# ---------------------------------------------------------------------------
# Not yours
# ---------------------------------------------------------------------------


def test_another_owners_library_is_a_404(as_owner, owner, stranger):
    """404 for both "no such memoir" and "not yours", as everywhere else."""
    other = str(stranger["memoir"]["id"])

    response = as_owner(str(owner["account"]["id"])).get(
        f"/memoirs/{other}/questions"
    )

    assert response.status_code == 404


def test_another_owners_question_cannot_be_edited(
    as_owner, owner, stranger, stub_gemini
):
    """The join up to `memoir` is where this is held, not a check in the route."""
    theirs = str(stranger["memoir"]["id"])
    library = as_owner(str(stranger["account"]["id"])).post(
        f"/memoirs/{theirs}/questions/generate", json={"notes": None}
    ).json()
    target = library["groups"][0]["questions"][0]

    response = as_owner(str(owner["account"]["id"])).patch(
        f"/questions/{target['id']}", json={"body": "Not mine to change."}
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# The contributor's side
# ---------------------------------------------------------------------------


def test_a_contributor_gets_their_own_groups_questions(
    client, factory, owner, as_owner, stub_gemini
):
    """What you ask a widow is not what you ask a colleague.

    The reason the library is grouped at all — and the reason the contribute
    form had to start asking, since every contributor used to be 'other'.
    """
    memoir_id = str(owner["memoir"]["id"])
    as_owner(str(owner["account"]["id"])).post(
        f"/memoirs/{memoir_id}/questions/generate", json={"notes": None}
    )

    grandchild = factory.contributor(
        memoir_id, display_name="Sana", relationship="grandchild"
    )

    response = client.get(
        f"/j/{owner['memoir']['link_token']}/questions",
        headers={"X-Participant-Token": grandchild["contributor_token"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["relationship"] == "grandchild"
    assert all("grandchild" in q for q in body["questions"])


def test_standard_mode_shows_the_shipped_set(client, factory, owner, as_owner, stub_gemini):
    """The mode is the answer, not a hint.

    An owner who drafted a library and then chose 'standard' gets standard.
    Falling back on "do custom rows exist" would make the choice advisory.
    """
    memoir_id = str(owner["memoir"]["id"])
    admin = as_owner(str(owner["account"]["id"]))
    admin.post(f"/memoirs/{memoir_id}/questions/generate", json={"notes": None})
    admin.patch(f"/memoirs/{memoir_id}/questions/mode", json={"mode": "standard"})

    friend = factory.contributor(
        memoir_id, display_name="Yusuf", relationship="friend"
    )

    response = client.get(
        f"/j/{owner['memoir']['link_token']}/questions",
        headers={"X-Participant-Token": friend["contributor_token"]},
    )

    assert response.json()["questions"] == standard_for(
        "friend", owner["memoir"]["subject_name"]
    )


def test_a_group_with_no_custom_questions_still_gets_asked_something(
    client, factory, owner, as_owner, stub_gemini
):
    """The blank page is the problem this whole feature exists to solve.

    An owner may delete a group they thought nobody would use. A cousin
    arriving to nothing is the failure, not the empty group.
    """
    memoir_id = str(owner["memoir"]["id"])
    admin = as_owner(str(owner["account"]["id"]))
    library = admin.post(
        f"/memoirs/{memoir_id}/questions/generate", json={"notes": None}
    ).json()

    friends = next(g for g in library["groups"] if g["relationship"] == "friend")
    for question in friends["questions"]:
        admin.delete(f"/questions/{question['id']}")

    friend = factory.contributor(
        memoir_id, display_name="Yusuf", relationship="friend"
    )
    response = client.get(
        f"/j/{owner['memoir']['link_token']}/questions",
        headers={"X-Participant-Token": friend["contributor_token"]},
    )

    assert response.json()["questions"] == standard_for(
        "friend", owner["memoir"]["subject_name"]
    )


def test_a_contributor_learns_nothing_about_the_library(
    client, factory, owner, as_owner, stub_gemini
):
    """`ContributorQuestions` is the line, and this is where it is checked.

    No ids for rows they cannot edit, no `source` telling them which questions
    a model wrote, no mode, and above all not `subject_notes` — the owner's
    own words about the person, written to produce questions and not to be
    read by whoever the link was forwarded to.
    """
    memoir_id = str(owner["memoir"]["id"])
    as_owner(str(owner["account"]["id"])).post(
        f"/memoirs/{memoir_id}/questions/generate",
        json={"notes": "A private paragraph about my grandmother."},
    )

    contributor = factory.contributor(memoir_id, relationship="child")
    body = client.get(
        f"/j/{owner['memoir']['link_token']}/questions",
        headers={"X-Participant-Token": contributor["contributor_token"]},
    ).json()

    assert set(body) == {"relationship", "questions"}
    assert "A private paragraph" not in response_text(body)


def test_a_participant_token_from_another_memoir_decides_nothing(
    client, factory, owner, stranger
):
    """The token is matched together with the memoir, never on its own.

    It no longer 404s — the link alone entitles you to be asked something, and
    a stale token is how a browser that contributed to one memoir arrives at
    another. What it must not do is carry that person's group across.
    """
    theirs = factory.contributor(
        str(stranger["memoir"]["id"]), relationship="spouse_partner"
    )

    response = client.get(
        f"/j/{owner['memoir']['link_token']}/questions",
        headers={"X-Participant-Token": theirs["contributor_token"]},
    )

    assert response.status_code == 200
    assert response.json()["relationship"] == "other"


def test_the_questions_are_readable_before_anybody_has_contributed(client, owner):
    """No participant token, because there is no participant yet.

    This is the visit the library exists for. Requiring a token meant it first
    appeared after the memory it was meant to prompt had already been written.
    """
    response = client.get(f"/j/{owner['memoir']['link_token']}/questions")

    assert response.status_code == 200
    assert response.json()["questions"]


def test_what_they_just_said_beats_what_is_stored(client, factory, owner):
    """Tapping a chip changes the questions, before anything is submitted."""
    contributor = factory.contributor(
        str(owner["memoir"]["id"]), relationship="child"
    )

    body = client.get(
        f"/j/{owner['memoir']['link_token']}/questions?relationship=friend",
        headers={"X-Participant-Token": contributor["contributor_token"]},
    ).json()

    assert body["relationship"] == "friend"


def test_a_relationship_that_is_not_one_falls_back(client, owner):
    """A hand-edited query string cannot reach anything but a real group."""
    body = client.get(
        f"/j/{owner['memoir']['link_token']}/questions?relationship=owner"
    ).json()

    assert body["relationship"] == "other"


def test_a_revoked_link_asks_nobody_anything(client, factory, owner):
    """Unknown, revoked and wrong-scope are one indistinguishable 404."""
    memoir_id = str(owner["memoir"]["id"])
    contributor = factory.contributor(memoir_id)
    factory.revoke_link(str(owner["memoir"]["link_id"]))

    response = client.get(
        f"/j/{owner['memoir']['link_token']}/questions",
        headers={"X-Participant-Token": contributor["contributor_token"]},
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# The contribute form now says who somebody is
# ---------------------------------------------------------------------------


def test_contributing_records_the_relationship(client, owner, db_cursor):
    """Every contributor used to be written as 'other', hardcoded.

    Invisible until two things read it: the reader's credit lines, which
    printed "other" under everybody's name, and the question library.
    """
    response = client.post(
        f"/j/{owner['memoir']['link_token']}/memories",
        json={
            "body_text": "She let me weigh out the sweets.",
            "display_name": "Sana",
            "relationship": "grandchild",
        },
    )

    assert response.status_code == 201

    db_cursor.execute(
        """
        SELECT relationship::text AS relationship
          FROM memoir_participant
         WHERE memoir_id = %(memoir)s AND display_name = 'Sana'
        """,
        {"memoir": str(owner["memoir"]["id"])},
    )
    assert db_cursor.fetchone()["relationship"] == "grandchild"


def test_not_saying_leaves_you_as_other(client, owner, db_cursor):
    """'other' is still the right answer for somebody who did not say.

    What changed is that it is no longer what everybody is.
    """
    client.post(
        f"/j/{owner['memoir']['link_token']}/memories",
        json={"body_text": "A memory.", "display_name": "Nobody In Particular"},
    )

    db_cursor.execute(
        """
        SELECT relationship::text AS relationship
          FROM memoir_participant
         WHERE memoir_id = %(memoir)s
           AND display_name = 'Nobody In Particular'
        """,
        {"memoir": str(owner["memoir"]["id"])},
    )
    assert db_cursor.fetchone()["relationship"] == "other"


def test_a_returning_contributor_can_correct_their_relationship(
    client, owner, db_cursor
):
    """Written through on a return visit, like the name already is.

    Somebody who picked "Friend" the first time and "Cousin" the second meant
    the second one.
    """
    token = owner["memoir"]["link_token"]
    first = client.post(
        f"/j/{token}/memories",
        json={
            "body_text": "One memory.",
            "display_name": "Yusuf",
            "relationship": "friend",
        },
    ).json()

    client.post(
        f"/j/{token}/memories",
        json={
            "body_text": "Another memory.",
            "display_name": "Yusuf",
            "relationship": "other",
            "participant_token": first["participant_token"],
        },
    )

    db_cursor.execute(
        """
        SELECT relationship::text AS relationship, count(*) OVER () AS people
          FROM memoir_participant
         WHERE memoir_id = %(memoir)s AND display_name = 'Yusuf'
        """,
        {"memoir": str(owner["memoir"]["id"])},
    )
    row = db_cursor.fetchone()
    assert row["relationship"] == "other"
    # And they are still one person, not two.
    assert row["people"] == 1


def test_an_invented_relationship_is_refused_at_the_edge(client, owner):
    """422 naming the field, rather than a 22P02 from the enum cast."""
    response = client.post(
        f"/j/{owner['memoir']['link_token']}/memories",
        json={
            "body_text": "A memory.",
            "display_name": "Someone",
            "relationship": "second cousin twice removed",
        },
    )

    assert response.status_code == 422


def response_text(payload) -> str:
    """The whole response as one string, for "this must not appear anywhere"."""
    import json

    return json.dumps(payload)
