# The owner's conversation with the guide.
#
# The guide is stubbed on the consumer's own reference
# (`chat_service.planner.chat`), the way `test_plan.py` stubs the planner, so
# a test that forgets to stub reaches nothing. The planner is stubbed the same
# way for the turn that replans, because `generate_plan` is what a replan runs.

import pytest

from src.domain.chapters import assembly_service, chat_service, planner
from tests.api.test_plan import archive, planner_says  # noqa: F401 — fixtures
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.db]


@pytest.fixture
def guide_says(monkeypatch):
    """Install the guide's next reply. `None` means it could not answer."""

    def install(reply, replan=False, instructions=None, outline=None):
        seen = {}

        async def _fake(shape, plan, history, message):
            seen.update(shape=shape, plan=plan, history=history, message=message)
            if reply is None:
                return None
            return planner.ChatTurn(
                reply=reply,
                replan=replan,
                instructions_for_builder=instructions,
                outline=outline
                and [planner.OutlineChapter(number=n, title=t) for n, t in outline],
            )

        monkeypatch.setattr(chat_service.planner, "chat", _fake)
        return seen

    return install


def test_the_owner_can_ask_and_read_back(as_owner, archive, guide_says):
    guide_says("Two of the three memories are dated; a date on the third would help.")
    client = as_owner(archive["owner_id"])
    url = f"/memoirs/{archive['memoir_id']}/chat"

    sent = client.post(url, json={"body": "Why is it divided like this?"})
    listed = client.get(url)

    assert sent.status_code == 200
    reply = sent.json()["reply"]
    assert reply["role"] == "guide"
    assert reply["replanned"] is False
    assert sent.json()["plan"] is None

    assert listed.status_code == 200
    assert [m["role"] for m in listed.json()] == ["owner", "guide"]
    assert listed.json()[0]["body"] == "Why is it divided like this?"


def test_the_guide_never_sees_a_memory(as_owner, archive, guide_says):
    """Counts, titles and findings reach the guide. Words never do.

    The whole point of a third model call that talks to the owner is that it
    has nothing to fabricate from. Everything it is handed is checked here.
    """
    seen = guide_says("Noted.")
    client = as_owner(archive["owner_id"])

    client.post(f"/memoirs/{archive['memoir_id']}/chat", json={"body": "Hello"})

    handed = str(seen["shape"]) + str(seen["plan"]) + str(seen["history"])
    for memory in archive["memories"]:
        assert memory["body_text"] not in handed
    assert seen["shape"]["memories"] == 3
    assert seen["shape"]["dated"] == 3
    assert seen["history"] == []
    assert seen["message"] == "Hello"


def test_a_replan_runs_the_planner_and_returns_the_plan(
    as_owner, archive, guide_says, planner_says
):
    """The guide asked for the builder; the reply carries what it built.

    Including in words: the guide's reply was written before the builder ran,
    so the note the planner writes afterwards is appended to it. Without that
    the owner reads a promise and an outline that may not have changed.
    """
    planner_says("The war years", "After", guide="Two chapters now.")
    guide_says(
        "Planning again with the war years together. This takes a few minutes.",
        replan=True,
        instructions="Group every memory from 1939 to 1945 into one chapter.",
    )
    client = as_owner(archive["owner_id"])

    sent = client.post(
        f"/memoirs/{archive['memoir_id']}/chat",
        json={"body": "put the war years in one chapter"},
    )
    plan = client.get(f"/memoirs/{archive['memoir_id']}/plan")
    book = client.get(f"/memoirs/{archive['memoir_id']}/chapters")

    assert sent.status_code == 200
    assert sent.json()["reply"]["replanned"] is True
    body = sent.json()["reply"]["body"]
    assert "\n\nTwo chapters now." in body
    assert "1. The war years" in body and "2. After" in body
    assert [c["title"] for c in sent.json()["plan"]["chapters"]] == [
        "The war years",
        "After",
    ]
    assert plan.status_code == 200
    assert plan.json()["generated_at"] == sent.json()["plan"]["generated_at"]
    # And the book was written from it in the same breath: to the owner,
    # planning and building are one step.
    assert sent.json()["plan"]["assembled_at"] is not None
    assert [c["title"] for c in book.json()["chapters"]] == ["The war years", "After"]


def test_building_from_the_button_is_announced_in_the_chat(
    as_owner, archive, planner_says
):
    """The outline is something the guide said, not a panel beside the chat.

    So a plan made with the button reaches the transcript the same way one
    made in conversation does: numbered chapters, the note, the findings.
    """
    planner_says(
        "One",
        "Two",
        guide="Two chapters, where the life divides.",
        review={
            "revised": False,
            "findings": [
                {"kind": "thin", "chapter": "Two", "note": "One memory is a word.", "fixed": False}
            ],
        },
    )
    client = as_owner(archive["owner_id"])
    url = f"/memoirs/{archive['memoir_id']}"

    assert client.post(f"{url}/plan").status_code == 200
    messages = client.get(f"{url}/chat").json()

    assert [m["role"] for m in messages] == ["guide"]
    assert messages[0]["replanned"] is True
    body = messages[0]["body"]
    assert body.startswith("Two chapters, where the life divides.")
    assert "1. One" in body and "2. Two" in body
    assert "Two — One memory is a word." in body


def test_the_guide_can_rename_reorder_and_drop_chapters(
    as_owner, archive, guide_says, planner_says
):
    """An outline answer is the old outline screen, by number.

    The guide is shown numbered titles and answers with the chapters to keep,
    in order, by number. Ids never reach it. The result is applied through
    `plan_service.edit` and the book is rebuilt.
    """
    planner_says("One", "Two", "Three")
    client = as_owner(archive["owner_id"])
    url = f"/memoirs/{archive['memoir_id']}"
    assert client.post(f"{url}/plan").status_code == 200
    seen = guide_says(
        "Done: the last chapter is first and called Beginnings; the middle one is out.",
        outline=[(3, "Beginnings"), (1, "One")],
    )

    sent = client.post(f"{url}/chat", json={"body": "put the last chapter first, call it Beginnings, and drop the middle one"})

    assert sent.status_code == 200
    assert sent.json()["reply"]["replanned"] is True
    assert "1. Beginnings" in sent.json()["reply"]["body"]
    assert [c["title"] for c in sent.json()["plan"]["chapters"]] == ["Beginnings", "One"]
    assert sent.json()["plan"]["assembled_at"] is not None
    assert [c["title"] for c in client.get(f"{url}/chapters").json()["chapters"]] == [
        "Beginnings",
        "One",
    ]
    # What the guide was shown: numbers and titles, never an id.
    assert seen["plan"]["chapters"][0]["title"] == "One"


def test_an_outline_naming_a_chapter_that_is_not_there_changes_nothing(
    as_owner, archive, guide_says, planner_says
):
    planner_says("One", "Two")
    client = as_owner(archive["owner_id"])
    url = f"/memoirs/{archive['memoir_id']}"
    before = client.post(f"{url}/plan").json()
    guide_says("Moved chapter nine to the front.", outline=[(9, "Nine"), (1, "One")])

    sent = client.post(f"{url}/chat", json={"body": "move chapter nine first"})

    assert sent.status_code == 200
    assert sent.json()["reply"]["replanned"] is False
    assert sent.json()["plan"] is None
    assert "it is as it was" in sent.json()["reply"]["body"]
    assert client.get(f"{url}/plan").json()["chapters"] == before["chapters"]


def test_a_replan_that_fell_back_says_why(as_owner, archive, guide_says, planner_says):
    """The builder could not run. The owner reads why, here, not in a log."""
    planner_says(reason="The model could not be reached: refused (404)")
    guide_says("Planning again now.", replan=True, instructions="One chapter.")
    client = as_owner(archive["owner_id"])

    sent = client.post(
        f"/memoirs/{archive['memoir_id']}/chat", json={"body": "one chapter"}
    )

    assert sent.status_code == 200
    reply = sent.json()["reply"]
    assert reply["replanned"] is True
    assert "could not be reached" in reply["body"]
    assert sent.json()["plan"]["organised_by"] == "by_date"


def test_a_silent_guide_is_still_a_reply(as_owner, archive, guide_says):
    """Upstream away is not a 500, and the question is not lost."""
    guide_says(None)
    client = as_owner(archive["owner_id"])

    sent = client.post(
        f"/memoirs/{archive['memoir_id']}/chat", json={"body": "Are you there?"}
    )

    assert sent.status_code == 200
    assert sent.json()["reply"]["body"] == chat_service._SILENCE
    assert sent.json()["reply"]["replanned"] is False
    assert len(client.get(f"/memoirs/{archive['memoir_id']}/chat").json()) == 2


def test_a_stranger_cannot_read_or_write(as_owner, stranger, archive, guide_says):
    guide_says("Hello.")
    client = as_owner(str(stranger["account"]["id"]))
    url = f"/memoirs/{archive['memoir_id']}/chat"

    assert client.get(url).status_code == 404
    assert client.post(url, json={"body": "Hello"}).status_code == 404


def test_a_sealed_memoir_takes_no_messages(as_owner, factory, archive, guide_says):
    guide_says("Hello.")
    factory.publish(archive["memoir_id"])

    response = as_owner(archive["owner_id"]).post(
        f"/memoirs/{archive['memoir_id']}/chat", json={"body": "Hello"}
    )

    assert response.status_code == 409


def test_an_empty_message_is_refused(as_owner, archive, guide_says):
    guide_says("Hello.")
    response = as_owner(archive["owner_id"]).post(
        f"/memoirs/{archive['memoir_id']}/chat", json={"body": ""}
    )
    assert response.status_code == 422


def test_generate_plan_is_the_only_way_a_chat_replans(monkeypatch):
    """A guard on the wiring: a replan is `generate_plan`, not a second path."""
    assert chat_service.assembly_service.generate_plan is assembly_service.generate_plan
