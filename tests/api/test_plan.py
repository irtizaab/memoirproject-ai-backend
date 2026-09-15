"""The plan: what the model decided, before the book is written.

`test_assembly.py` covers the fallback, because `conftest` deliberately sets no
`GEMINI_API_KEY` and so every plan it makes is `by_date`. This file is the
other half — the planner path, with the model stubbed — plus the lifecycle the
plan row adds:

  - a plan is generated, read back, and only then assembled
  - assembling without a plan is refused, and says which step is missing
  - regenerating replaces the plan rather than adding one
  - a plan is nobody else's to read, generate, or assemble
  - a memory deleted after planning does not reach `block_source`

The stub is installed on `assembly_service.planner.plan` — the *consumer's*
reference, not the integration's — for the reason `test_questions.py` gives: a
test that forgets to stub then makes a real HTTP call and fails loudly, instead
of quietly reaching Google with whatever key is in `.env`.
"""

import uuid

import pytest

from src.domain.chapters import assembly_service, planner
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.db]


@pytest.fixture
def archive(factory, owner):
    """A memoir with three dated memories from two people."""
    memoir_id = str(owner["memoir"]["id"])

    margaret = factory.contributor(memoir_id, display_name="Margaret Reyes")
    thomas = factory.contributor(memoir_id, display_name="Thomas Marsh")

    first = factory.memory(
        memoir_id,
        margaret["id"],
        kind="text",
        title=None,
        body_text="The cedar planks had soaked in July heat all day long.",
        happened_on="1982-07-14",
    )
    second = factory.memory(
        memoir_id,
        thomas["id"],
        kind="text",
        title=None,
        body_text="He counted the ripple rings behind our canoe.",
        happened_on="1984-08-02",
    )
    third = factory.memory(
        memoir_id,
        margaret["id"],
        kind="text",
        title=None,
        body_text="The boat stayed docked through twenty-four winters.",
        happened_on="1996-01-09",
    )

    return {
        "memoir_id": memoir_id,
        "owner_id": str(owner["account"]["id"]),
        "memories": [first, second, third],
    }


@pytest.fixture
def planner_says(monkeypatch):
    """Install a stub plan, in the shape `planner.plan` returns.

    Returns a callable taking the chapter titles to produce. Each chapter holds
    one paragraph per memory in the archive, in that person's own words and
    attributed whole — the same shape `planner._clean` emits once verification
    has run, so nothing downstream can tell this from a real plan.

    Whole-block attribution rather than spans on purpose: an offset asserted
    here would be an assertion about this stub's arithmetic. `verify` is what
    computes real ones, and `tests/unit/test_planner.py` is where it is tested.

    `place_photographs=True` also places every photograph the planner was
    shown — the first inset, the rest as a carousel — which is what the figure
    tests need and what the prose tests should not have to think about.
    """

    def install(
        *titles, place_photographs=False, review=None, guide=None, reason=None
    ):
        async def _fake(
            memories, memoir, prose, photographs=None, instructions=None
        ):
            if reason is not None:
                # The builder could not: `chapters=None` sends the caller
                # to the decade fallback, and `reason` is what the owner reads.
                return planner.Outcome(None, reason=reason, guide=guide)
            usable = [m for m in memories if prose(m)]
            photographs = list(photographs or [])
            return planner.Outcome(review=review, guide=guide, chapters=[
                {
                    "title": title,
                    "from_year": 1982,
                    "through_year": 1996,
                    "blocks": [
                        {
                            "kind": "paragraph",
                            "text": prose(memory),
                            "sources": [
                                {
                                    "memory_id": memory["id"],
                                    "participant_id": memory["participant_id"],
                                    "start_offset": None,
                                    "end_offset": None,
                                    "diverges": False,
                                }
                            ],
                        }
                        for memory in usable
                    ],
                    # Every photograph the planner was shown, anchored to the
                    # first memory the chapter quotes. Off by default so the
                    # prose tests are not also figure tests.
                    "figures": (
                        [
                            {
                                "asset_id": photograph["id"],
                                "anchor_memory_id": usable[0]["id"],
                                "placement": "carousel"
                                if position
                                else "inset",
                            }
                            for position, photograph in enumerate(photographs)
                        ]
                        if place_photographs and usable
                        else []
                    ),
                    "memories": list(usable),
                }
                for title in titles
            ])

        monkeypatch.setattr(assembly_service.planner, "plan", _fake)

    return install


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_the_model_organises_the_book_and_says_so(as_owner, archive, planner_says):
    """The planner ran, and the response is honest about which one did.

    The load-bearing assertion of the whole feature. Without `organised_by` a
    decade-banded book and a planned one are indistinguishable once written,
    which is how a deployment with no key can produce the fallback for every
    memoir and never mention it.
    """
    planner_says("Summers at the lake")

    response = as_owner(archive["owner_id"]).post(
        f"/memoirs/{archive['memoir_id']}/plan"
    )

    assert response.status_code == 200
    plan = response.json()
    assert plan["organised_by"] == "planner"
    assert [chapter["title"] for chapter in plan["chapters"]] == [
        "Summers at the lake"
    ]
    assert plan["assembled_at"] is None
    assert plan["edited_at"] is None


def test_reviewer_findings_come_back_on_the_plan(as_owner, archive, planner_says):
    """What the reviewer found is stored with the plan and read back with it.

    The draft used to arrive with no account of itself. Now the findings
    travel inside the plan row, so the owner reads them under the outline and
    a regenerated plan replaces them along with the chapters.
    """
    planner_says(
        "The house",
        review={
            "findings": [
                {
                    "kind": "thin",
                    "chapter": None,
                    "note": "One memory is a single word and gave nothing to write from.",
                    "fixed": False,
                }
            ],
            "revised": False,
        },
    )
    owner = as_owner(archive["owner_id"])

    planned = owner.post(f"/memoirs/{archive['memoir_id']}/plan").json()
    fetched = owner.get(f"/memoirs/{archive['memoir_id']}/plan").json()

    assert planned["review"]["revised"] is False
    assert planned["review"]["findings"][0]["kind"] == "thin"
    assert fetched["review"] == planned["review"]


def test_the_guide_note_travels_with_the_plan(as_owner, archive, planner_says):
    planner_says("The house", guide="One memory is only a title; a few more words would help.")
    owner = as_owner(archive["owner_id"])

    planned = owner.post(f"/memoirs/{archive['memoir_id']}/plan").json()
    fetched = owner.get(f"/memoirs/{archive['memoir_id']}/plan").json()

    assert planned["guide"].startswith("One memory")
    assert fetched["guide"] == planned["guide"]


def test_a_plan_can_be_read_back_without_planning_again(
    as_owner, archive, planner_says
):
    """GET is free. It is a row, and the expensive call already happened."""
    planner_says("Summers at the lake")
    client = as_owner(archive["owner_id"])

    generated = client.post(f"/memoirs/{archive['memoir_id']}/plan").json()
    fetched = client.get(f"/memoirs/{archive['memoir_id']}/plan").json()

    assert fetched == generated


def test_an_unplanned_memoir_says_which_step_is_missing(as_owner, archive):
    """404 with a sentence, not a bare one.

    The owner has a real memoir and has simply not planned it. The frontend
    needs to tell those apart to offer the button that fixes it.
    """
    response = as_owner(archive["owner_id"]).get(
        f"/memoirs/{archive['memoir_id']}/plan"
    )

    assert response.status_code == 404
    assert "not been planned" in response.json()["detail"]


def test_planning_again_replaces_the_plan(as_owner, archive, planner_says):
    """One plan per memoir. Asking for another is asking to discard this one."""
    client = as_owner(archive["owner_id"])

    planner_says("Summers at the lake")
    client.post(f"/memoirs/{archive['memoir_id']}/plan")

    planner_says("The dock", "The winters after")
    second = client.post(f"/memoirs/{archive['memoir_id']}/plan").json()

    assert [chapter["title"] for chapter in second["chapters"]] == [
        "The dock",
        "The winters after",
    ]

    # And the row, not just the response.
    fetched = client.get(f"/memoirs/{archive['memoir_id']}/plan").json()
    assert len(fetched["chapters"]) == 2


# ---------------------------------------------------------------------------
# Assembling what was planned
# ---------------------------------------------------------------------------


def test_assembling_without_a_plan_is_refused(as_owner, archive):
    """409 and the order of operations, not 400 and "no memories".

    There are memories. What is missing is the step in between, and saying so
    is the difference between a button the owner can press and an error.
    """
    response = as_owner(archive["owner_id"]).post(
        f"/memoirs/{archive['memoir_id']}/assemble"
    )

    assert response.status_code == 409
    assert "plan the memoir" in response.json()["detail"]


def test_the_book_is_built_from_the_plan_that_was_stored(
    as_owner, archive, planner_says
):
    """The model's chapter title reaches the finished book, unaltered.

    End to end through the routes the family use: the title the planner chose
    is what the contents rail prints, and `organised_by` survives into the
    assembly result.
    """
    planner_says("Summers at the lake")
    client = as_owner(archive["owner_id"])

    client.post(f"/memoirs/{archive['memoir_id']}/plan")
    result = client.post(f"/memoirs/{archive['memoir_id']}/assemble").json()

    assert result["organised_by"] == "planner"
    assert result["chapters"] == 1
    assert result["blocks"] == 3
    assert result["sources"] == 3

    reading = client.get(f"/memoirs/{archive['memoir_id']}/chapters").json()
    assert [chapter["title"] for chapter in reading["chapters"]] == [
        "Summers at the lake"
    ]


def test_assembling_records_that_the_plan_was_used(
    as_owner, archive, planner_says
):
    """`assembled_at` is how the frontend knows it is showing a record.

    Before it is set, the plan is a draft the owner may still change. After, it
    describes the book that exists downstream of it.
    """
    planner_says("Summers at the lake")
    client = as_owner(archive["owner_id"])

    client.post(f"/memoirs/{archive['memoir_id']}/plan")
    assert client.get(f"/memoirs/{archive['memoir_id']}/plan").json()[
        "assembled_at"
    ] is None

    client.post(f"/memoirs/{archive['memoir_id']}/assemble")

    assert client.get(f"/memoirs/{archive['memoir_id']}/plan").json()[
        "assembled_at"
    ] is not None


# ---------------------------------------------------------------------------
# Correcting it
# ---------------------------------------------------------------------------


def _edit(plan, **changes):
    """The plan's chapters, ready to send back, with `changes` applied to the first.

    PATCH takes the whole chapter list, so this is what a screen would submit:
    everything as it came back, with one thing different.
    """
    chapters = [dict(chapter) for chapter in plan["chapters"]]
    chapters[0] = {**chapters[0], **changes}
    return {"chapters": chapters}


def test_the_owner_can_rename_a_chapter(as_owner, archive, planner_says):
    """The first correction anybody will make. The model titles a chapter and
    the family knows what it was really called."""
    planner_says("Summers at the lake")
    client = as_owner(archive["owner_id"])
    plan = client.post(f"/memoirs/{archive['memoir_id']}/plan").json()

    response = client.patch(
        f"/memoirs/{archive['memoir_id']}/plan",
        json=_edit(plan, title="The dock, and after"),
    )

    assert response.status_code == 200
    edited = response.json()
    assert edited["chapters"][0]["title"] == "The dock, and after"
    assert edited["edited_at"] is not None

    # And into the book, which is the point.
    client.post(f"/memoirs/{archive['memoir_id']}/assemble")
    reading = client.get(f"/memoirs/{archive['memoir_id']}/chapters").json()
    assert reading["chapters"][0]["title"] == "The dock, and after"


def test_chapters_can_be_reordered(as_owner, archive, planner_says):
    """Array order is reading order, and the ordinals are renumbered here."""
    planner_says("First", "Second")
    client = as_owner(archive["owner_id"])
    plan = client.post(f"/memoirs/{archive['memoir_id']}/plan").json()

    flipped = list(reversed(plan["chapters"]))
    response = client.patch(
        f"/memoirs/{archive['memoir_id']}/plan", json={"chapters": flipped}
    )

    assert response.status_code == 200
    assert [c["title"] for c in response.json()["chapters"]] == ["Second", "First"]

    client.post(f"/memoirs/{archive['memoir_id']}/assemble")
    reading = client.get(f"/memoirs/{archive['memoir_id']}/chapters").json()
    assert [c["title"] for c in reading["chapters"]] == ["Second", "First"]
    assert [c["ordinal"] for c in reading["chapters"]] == [0, 1]


def test_a_chapter_can_be_dropped(as_owner, archive, planner_says):
    """Leaving it out is how it is removed. Its memories stay in the archive."""
    planner_says("Keep", "Drop")
    client = as_owner(archive["owner_id"])
    plan = client.post(f"/memoirs/{archive['memoir_id']}/plan").json()

    response = client.patch(
        f"/memoirs/{archive['memoir_id']}/plan",
        json={"chapters": [plan["chapters"][0]]},
    )

    assert response.status_code == 200
    assert [c["title"] for c in response.json()["chapters"]] == ["Keep"]

    # Still in the archive — a chapter was dropped, not the memories.
    assert len(client.get(f"/memoirs/{archive['memoir_id']}/memories").json()) == 3


def test_a_chapter_the_plan_does_not_hold_is_refused(as_owner, archive, planner_says):
    """400, not 409: the request is malformed rather than conflicting.

    In practice it means the screen is looking at a plan that has since been
    regenerated, and the ids it holds belong to chapters nobody has any more.
    """
    planner_says("Summers at the lake")
    client = as_owner(archive["owner_id"])
    plan = client.post(f"/memoirs/{archive['memoir_id']}/plan").json()

    response = client.patch(
        f"/memoirs/{archive['memoir_id']}/plan",
        json=_edit(plan, id=str(uuid.uuid4())),
    )

    assert response.status_code == 400


def test_an_edit_cannot_empty_the_plan(as_owner, archive, planner_says):
    """A plan with nothing in it would leave the owner able only to regenerate."""
    planner_says("Summers at the lake")
    client = as_owner(archive["owner_id"])
    client.post(f"/memoirs/{archive['memoir_id']}/plan")

    response = client.patch(
        f"/memoirs/{archive['memoir_id']}/plan", json={"chapters": []}
    )

    # 422 from the model's own `min_length`, before the domain is reached.
    assert response.status_code == 422


def test_an_assembled_plan_is_still_editable(as_owner, archive, planner_says):
    """It used to be a 409, and that was wrong about which fact protects what.

    Assembly is not what makes a character offset permanent — publication is.
    An unsealed book has no readers and no comment layer, and `assemble` writes
    its chapters from scratch every time, so the outline stays a draft until
    the memoir is sealed and assembling again is how a changed outline reaches
    the page. The cost belongs in the interface: reassembling replaces anything
    corrected by hand on the page itself.
    """
    planner_says("Summers at the lake")
    client = as_owner(archive["owner_id"])
    plan = client.post(f"/memoirs/{archive['memoir_id']}/plan").json()
    client.post(f"/memoirs/{archive['memoir_id']}/assemble")

    response = client.patch(
        f"/memoirs/{archive['memoir_id']}/plan",
        json=_edit(plan, title="Summers at the lake, after all"),
    )

    assert response.status_code == 200
    assert response.json()["chapters"][0]["title"] == "Summers at the lake, after all"


def test_reassembling_applies_an_edited_outline(as_owner, archive, planner_says):
    """The other half of the same rule: the edit reaches the book.

    A plan that can be edited but not applied would be a form that throws away
    what you type.
    """
    planner_says("Summers at the lake")
    client = as_owner(archive["owner_id"])
    memoir_id = archive["memoir_id"]

    plan = client.post(f"/memoirs/{memoir_id}/plan").json()
    client.post(f"/memoirs/{memoir_id}/assemble")

    client.patch(f"/memoirs/{memoir_id}/plan", json=_edit(plan, title="The dock"))
    client.post(f"/memoirs/{memoir_id}/assemble")

    chapters = client.get(f"/memoirs/{memoir_id}/chapters").json()["chapters"]
    assert [chapter["title"] for chapter in chapters] == ["The dock"]


def test_a_published_memoir_refuses_every_kind_of_edit(
    as_owner, factory, archive, planner_says
):
    """The immutability rule, at the plan layer as well as the book layer."""
    planner_says("Summers at the lake")
    client = as_owner(archive["owner_id"])
    plan = client.post(f"/memoirs/{archive['memoir_id']}/plan").json()

    factory.publish(archive["memoir_id"])

    response = client.patch(
        f"/memoirs/{archive['memoir_id']}/plan", json=_edit(plan, title="No")
    )

    assert response.status_code == 409


def test_a_stranger_cannot_edit_a_plan(as_owner, stranger, archive, planner_says):
    """404, not 403 — and not a 409 either, which would confirm it exists."""
    planner_says("Summers at the lake")
    plan = as_owner(archive["owner_id"]).post(
        f"/memoirs/{archive['memoir_id']}/plan"
    ).json()

    response = as_owner(str(stranger["account"]["id"])).patch(
        f"/memoirs/{archive['memoir_id']}/plan", json=_edit(plan, title="Mine now")
    )

    assert response.status_code == 404


def test_editing_a_passage_keeps_the_attribution_it_can_and_drops_what_it_cannot(
    as_owner, archive, planner_says
):
    """The rule the whole editing feature rests on, end to end.

    A source's span is character offsets into the passage text. The owner
    rewrites the passage; the span is re-found if its words survived, and set
    to whole-block if they did not. What must never happen is an offset left
    pointing into text that moved — publication is immutable, so it would be
    wrong forever, and it would credit one person's clause to another.
    """
    planner_says("Summers at the lake")
    client = as_owner(archive["owner_id"])
    plan = client.post(f"/memoirs/{archive['memoir_id']}/plan").json()

    chapter = dict(plan["chapters"][0])
    blocks = [dict(block) for block in chapter["blocks"]]
    # The stub attributes whole blocks, so this asserts the shape survives an
    # edit rather than the arithmetic — which `tests/unit/test_plan_service.py`
    # covers against hand-built spans.
    blocks[0] = {**blocks[0], "text": "Rewritten by the owner, entirely."}
    chapter["blocks"] = blocks

    response = client.patch(
        f"/memoirs/{archive['memoir_id']}/plan",
        json={"chapters": [chapter, *plan["chapters"][1:]]},
    )

    assert response.status_code == 200
    edited = response.json()["chapters"][0]["blocks"][0]
    assert edited["text"] == "Rewritten by the owner, entirely."
    # Still attributed. An unattributed passage would have been dropped.
    assert edited["sources"]

    # And the owner's words are what the book says.
    client.post(f"/memoirs/{archive['memoir_id']}/assemble")
    reading = client.get(f"/memoirs/{archive['memoir_id']}/chapters").json()
    read = client.get(f"/chapters/{reading['chapters'][0]['id']}").json()
    assert read["blocks"][0]["text"] == "Rewritten by the owner, entirely."


# ---------------------------------------------------------------------------
# Photographs
# ---------------------------------------------------------------------------


@pytest.fixture
def with_photographs(factory, archive):
    """Two photographs on the archive's first memory, uploaded and confirmed."""
    memoir_id = archive["memoir_id"]
    memory_id = archive["memories"][0]["id"]

    first = factory.asset(memoir_id, memory_id=memory_id, kind="image")
    second = factory.asset(memoir_id, memory_id=memory_id, kind="image")

    return [first, second]


def test_the_model_places_the_photographs_it_was_shown(
    as_owner, archive, with_photographs, planner_says
):
    """Both photographs reach the book, in the placements the plan chose.

    Note what is *not* asserted: that the planner saw the pixels. Storage is
    unreachable in tests, so `_load_images` drops every image and hands the
    model none — which is exactly why the stub is what places them here. What
    this proves is the path from a placement decision to a `chapter_block`
    row, including the carousel value migration 0016 added.
    """
    planner_says("Summers at the lake", place_photographs=True)
    client = as_owner(archive["owner_id"])

    plan = client.post(f"/memoirs/{archive['memoir_id']}/plan").json()
    placements = [f["placement"] for f in plan["chapters"][0]["figures"]]
    assert placements == ["inset", "carousel"]

    result = client.post(f"/memoirs/{archive['memoir_id']}/assemble").json()
    assert result["figures"] == 2

    reading = client.get(f"/memoirs/{archive['memoir_id']}/chapters").json()
    read = client.get(f"/chapters/{reading['chapters'][0]['id']}").json()
    figures = [b for b in read["blocks"] if b["kind"] == "figure"]
    assert {f["figure"]["placement"] for f in figures} == {"inset", "carousel"}

    # Every figure anchors to a paragraph, which the schema requires and the
    # reader's geometry depends on.
    paragraphs = {b["id"] for b in read["blocks"] if b["kind"] == "paragraph"}
    assert all(f["figure"]["anchor_block_id"] in paragraphs for f in figures)


def test_a_photograph_deleted_after_planning_does_not_break_assembly(
    as_owner, archive, with_photographs, planner_says
):
    """`chapter_block` foreign-keys the asset, so this is a 500 if unhandled."""
    planner_says("Summers at the lake", place_photographs=True)
    client = as_owner(archive["owner_id"])

    client.post(f"/memoirs/{archive['memoir_id']}/plan")
    removed = client.delete(
        f"/memories/{archive['memories'][0]['id']}/assets/{with_photographs[0]['id']}"
    )
    assert removed.status_code in (200, 204)

    result = client.post(f"/memoirs/{archive['memoir_id']}/assemble")

    assert result.status_code == 200
    assert result.json()["figures"] == 1


# ---------------------------------------------------------------------------
# The archive moves on
# ---------------------------------------------------------------------------


def test_a_memory_deleted_after_planning_drops_out_of_the_book(
    as_owner, archive, planner_says
):
    """The plan cites memory ids; the archive is the authority on which exist.

    A plan holding a deleted memory would insert a `block_source` pointing at
    nothing — a foreign key violation, and a 500 on a button the owner pressed
    twice. `rehydrate` drops the source, then the block that has no source
    left, which is the same rule an unattributed paragraph gets everywhere
    else in this product.
    """
    planner_says("Summers at the lake")
    client = as_owner(archive["owner_id"])

    client.post(f"/memoirs/{archive['memoir_id']}/plan")

    gone = client.delete(f"/memories/{archive['memories'][0]['id']}")
    assert gone.status_code in (200, 204)

    result = client.post(f"/memoirs/{archive['memoir_id']}/assemble").json()

    # Two paragraphs, not three, and no error.
    assert result["blocks"] == 2
    assert result["sources"] == 2


def test_a_plan_whose_every_memory_is_gone_is_not_a_book(
    as_owner, archive, planner_says
):
    """Nothing left to write is 400 and a sentence, not zero chapters and a 200."""
    planner_says("Summers at the lake")
    client = as_owner(archive["owner_id"])

    client.post(f"/memoirs/{archive['memoir_id']}/plan")
    for memory in archive["memories"]:
        client.delete(f"/memories/{memory['id']}")

    response = client.post(f"/memoirs/{archive['memoir_id']}/assemble")

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Whose plan it is
# ---------------------------------------------------------------------------


def test_a_stranger_cannot_plan_someone_elses_memoir(
    as_owner, stranger, archive, planner_says
):
    """404, not 403 — and it never reaches the model.

    Generating is the one route in this file that costs money. An unauthorized
    caller must be turned away before it is spent, which is why the ownership
    check is the first thing `generate_plan` does.
    """
    planner_says("Summers at the lake")
    client = as_owner(str(stranger["account"]["id"]))

    assert client.post(f"/memoirs/{archive['memoir_id']}/plan").status_code == 404
    assert client.get(f"/memoirs/{archive['memoir_id']}/plan").status_code == 404


def test_a_stranger_cannot_read_a_plan_that_exists(
    as_owner, stranger, archive, planner_says
):
    """A plan that is real and not yours answers exactly as one that is not."""
    planner_says("Summers at the lake")
    as_owner(archive["owner_id"]).post(f"/memoirs/{archive['memoir_id']}/plan")

    response = as_owner(str(stranger["account"]["id"])).get(
        f"/memoirs/{archive['memoir_id']}/plan"
    )

    assert response.status_code == 404


def test_a_memoir_that_does_not_exist_answers_the_same_way(as_owner, archive):
    """Unknown and not-yours are one response, on both plan routes."""
    client = as_owner(archive["owner_id"])

    assert client.post(f"/memoirs/{uuid.uuid4()}/plan").status_code == 404
    assert client.get(f"/memoirs/{uuid.uuid4()}/plan").status_code == 404
