"""The page, corrected by hand: PATCH /chapters/{id}.

The owner reads their assembled memoir before sealing it and changes what they
find — a title, a sentence, the order of a page, where a photograph sits, or
whether it is there at all. `page_service.py` is what does it.

The test that matters most is `test_a_reworded_passage_keeps_its_credit`:
`block_source` spans are character offsets, so editing the words moves them,
and a moved offset credits the wrong person permanently once the memoir is
sealed. Everything else here is the boundary — whose chapter it is, and what a
sealed memoir refuses.
"""

import pytest

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.db]


@pytest.fixture
def page(factory, owner):
    """One chapter: two paragraphs with real spans, and a photograph.

    The first paragraph is deliberately a composite — two people, one of them
    credited for an exact clause — because that clause is what an edit has to
    keep pointing at.
    """
    memoir_id = str(owner["memoir"]["id"])

    margaret = factory.contributor(memoir_id, display_name="Margaret Reyes")
    claire = factory.contributor(memoir_id, display_name="Claire Donnelly")

    hers = factory.memory(
        memoir_id,
        margaret["id"],
        kind="text",
        title=None,
        body_text="Three of its hammers had been re-felted.",
    )
    theirs = factory.memory(
        memoir_id,
        claire["id"],
        kind="text",
        title=None,
        body_text="She came back to the keys in the spring.",
    )

    chapter = factory.chapter(memoir_id, ordinal=0, title="The House on Ellsworth Lane")
    chapter_id = str(chapter["id"])

    # "The Chickering was not a good instrument. Three of its hammers had been
    #  0                                         42
    text = (
        "The Chickering was not a good instrument. "
        "Three of its hammers had been re-felted."
    )
    clause = "Three of its hammers had been re-felted."
    first = factory.block(memoir_id, chapter_id, ordinal=0, text=text)
    second = factory.block(
        memoir_id,
        chapter_id,
        ordinal=1,
        text="She came back to the keys in the spring.",
    )

    factory.source(
        memoir_id,
        str(first["id"]),
        memory_id=str(hers["id"]),
        participant_id=str(margaret["id"]),
        start_offset=text.index(clause),
        end_offset=text.index(clause) + len(clause),
    )
    factory.source(
        memoir_id,
        str(second["id"]),
        memory_id=str(theirs["id"]),
        participant_id=str(claire["id"]),
    )

    asset = factory.asset(memoir_id, memory_id=str(theirs["id"]), kind="image")
    figure = factory.figure(
        memoir_id,
        chapter_id,
        asset_id=str(asset["id"]),
        anchor_block_id=str(first["id"]),
        ordinal=2,
        placement="margin",
    )

    return {
        "memoir_id": memoir_id,
        "owner_id": str(owner["account"]["id"]),
        "chapter_id": chapter_id,
        "first": str(first["id"]),
        "second": str(second["id"]),
        "figure": str(figure["id"]),
        "text": text,
        "clause": clause,
    }


def patch(as_owner, page, body: dict, user_id: str | None = None):
    return as_owner(user_id or page["owner_id"]).patch(
        f"/chapters/{page['chapter_id']}", json=body
    )


def blocks_of(response) -> list[dict]:
    return response.json()["blocks"]


# ---------------------------------------------------------------------------
# The title
# ---------------------------------------------------------------------------


def test_the_owner_renames_a_chapter(as_owner, page):
    response = patch(as_owner, page, {"title": "A Room with a North Window"})

    assert response.status_code == 200
    assert response.json()["title"] == "A Room with a North Window"


def test_a_blank_title_is_ignored_rather_than_stored(as_owner, page):
    # `chapter_title_not_blank` would refuse it, and somebody who cleared the
    # field meant to type something.
    response = patch(as_owner, page, {"title": "   "})

    assert response.status_code == 200
    assert response.json()["title"] == "The House on Ellsworth Lane"


def test_renaming_leaves_the_page_alone(as_owner, page):
    response = patch(as_owner, page, {"title": "Whatever"})

    assert [block["text"] for block in blocks_of(response) if block["text"]] == [
        page["text"],
        "She came back to the keys in the spring.",
    ]


# ---------------------------------------------------------------------------
# The words, and the credit underneath them
# ---------------------------------------------------------------------------


def test_the_owner_rewords_a_passage(as_owner, page):
    response = patch(
        as_owner,
        page,
        {
            "blocks": [
                {"id": page["first"], "text": "The Chickering was a poor instrument. "
                 + page["clause"]},
                {"id": page["second"]},
                {"id": page["figure"]},
            ]
        },
    )

    assert response.status_code == 200
    first = next(b for b in blocks_of(response) if b["id"] == page["first"])
    assert first["text"].startswith("The Chickering was a poor instrument.")


def test_a_reworded_passage_keeps_its_credit(as_owner, page):
    """The whole reason this endpoint re-surveys instead of just writing.

    Margaret is credited for one clause by character offset. Shortening the
    sentence in front of it moves those characters; the span has to follow the
    words rather than stay on the numbers.
    """
    rewritten = "It was a poor instrument. " + page["clause"]

    response = patch(
        as_owner,
        page,
        {
            "blocks": [
                {"id": page["first"], "text": rewritten},
                {"id": page["second"]},
                {"id": page["figure"]},
            ]
        },
    )

    first = next(b for b in blocks_of(response) if b["id"] == page["first"])
    source = next(s for s in first["sources"] if s["name"] == "Margaret Reyes")

    assert source["start_offset"] == rewritten.index(page["clause"])
    assert source["end_offset"] == len(rewritten)
    assert rewritten[source["start_offset"] : source["end_offset"]] == page["clause"]


def test_a_credit_whose_words_are_gone_falls_back_to_the_whole_passage(
    as_owner, page
):
    # Deleting the clause cannot delete Margaret. She still supplied part of
    # this paragraph; what is lost is only which part.
    response = patch(
        as_owner,
        page,
        {
            "blocks": [
                {"id": page["first"], "text": "The Chickering was not a good instrument."},
                {"id": page["second"]},
                {"id": page["figure"]},
            ]
        },
    )

    first = next(b for b in blocks_of(response) if b["id"] == page["first"])
    source = next(s for s in first["sources"] if s["name"] == "Margaret Reyes")

    assert source["start_offset"] is None
    assert source["end_offset"] is None


def test_a_blank_passage_is_refused(as_owner, page):
    response = patch(as_owner, page, {"blocks": [{"id": page["first"], "text": "  "}]})

    assert response.status_code == 400
    assert "empty" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Order, and leaving things out
# ---------------------------------------------------------------------------


def test_the_page_is_reordered_by_array_position(as_owner, page):
    response = patch(
        as_owner,
        page,
        {
            "blocks": [
                {"id": page["second"]},
                {"id": page["first"]},
                {"id": page["figure"]},
            ]
        },
    )

    assert response.status_code == 200
    order = [block["id"] for block in blocks_of(response)]
    assert order == [page["second"], page["first"], page["figure"]]
    assert [block["ordinal"] for block in blocks_of(response)] == [0, 1, 2]


def test_a_passage_left_out_is_removed(as_owner, page):
    response = patch(
        as_owner,
        page,
        {"blocks": [{"id": page["first"]}, {"id": page["figure"]}]},
    )

    assert response.status_code == 200
    assert page["second"] not in [block["id"] for block in blocks_of(response)]


def test_removing_a_paragraph_takes_the_photograph_anchored_to_it(as_owner, page):
    # The anchor cascade, made explicit: a figure cannot exist without the
    # paragraph it sits beside, so leaving that paragraph out takes it too. The
    # photograph is still in the archive.
    response = patch(as_owner, page, {"blocks": [{"id": page["second"]}]})

    assert response.status_code == 200
    assert [block["id"] for block in blocks_of(response)] == [page["second"]]


def test_a_page_cannot_be_emptied(as_owner, page):
    response = patch(as_owner, page, {"blocks": []})

    assert response.status_code == 409
    assert "at least one passage" in response.json()["detail"]


def test_a_passage_this_chapter_does_not_hold_is_refused(as_owner, page, factory):
    other = factory.chapter(page["memoir_id"], ordinal=1, title="Elsewhere")
    elsewhere = factory.block(
        page["memoir_id"], str(other["id"]), ordinal=0, text="A different chapter."
    )

    response = patch(
        as_owner,
        page,
        {"blocks": [{"id": page["first"]}, {"id": str(elsewhere["id"])}]},
    )

    # Adding prose is the thing this endpoint must never do: a passage with no
    # source behind it is a fabricated one.
    assert response.status_code == 400
    assert "no such passage" in response.json()["detail"]


def test_the_same_passage_twice_is_refused(as_owner, page):
    response = patch(
        as_owner,
        page,
        {"blocks": [{"id": page["first"]}, {"id": page["first"]}]},
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Photographs
# ---------------------------------------------------------------------------


def test_a_photograph_is_moved_to_another_paragraph(as_owner, page):
    response = patch(
        as_owner,
        page,
        {
            "blocks": [
                {"id": page["first"]},
                {"id": page["second"]},
                {
                    "id": page["figure"],
                    "anchor_block_id": page["second"],
                    "placement": "inset",
                },
            ]
        },
    )

    assert response.status_code == 200
    figure = next(b for b in blocks_of(response) if b["id"] == page["figure"])
    assert figure["figure"]["anchor_block_id"] == page["second"]
    assert figure["figure"]["placement"] == "inset"


def test_a_photograph_cannot_be_anchored_to_a_removed_paragraph(as_owner, page):
    response = patch(
        as_owner,
        page,
        {
            "blocks": [
                {"id": page["second"]},
                {"id": page["figure"], "anchor_block_id": page["first"]},
            ]
        },
    )

    assert response.status_code == 400
    assert "paragraph" in response.json()["detail"]


def test_a_photograph_cannot_be_anchored_to_a_pulled_line(as_owner, page, factory):
    pull = factory.block(
        page["memoir_id"],
        page["chapter_id"],
        ordinal=3,
        kind="pull",
        text="Both accounts are kept.",
    )

    response = patch(
        as_owner,
        page,
        {
            "blocks": [
                {"id": page["first"]},
                {"id": page["second"]},
                {"id": str(pull["id"])},
                {"id": page["figure"], "anchor_block_id": str(pull["id"])},
            ]
        },
    )

    # One lifted sentence would caption a photograph with a fragment.
    assert response.status_code == 400


def test_a_photograph_is_left_out_without_touching_the_prose(as_owner, page):
    response = patch(
        as_owner,
        page,
        {"blocks": [{"id": page["first"]}, {"id": page["second"]}]},
    )

    assert response.status_code == 200
    kinds = [block["kind"] for block in blocks_of(response)]
    assert "figure" not in kinds
    assert kinds == ["paragraph", "paragraph"]


# ---------------------------------------------------------------------------
# Whose chapter it is, and when nothing may change at all
# ---------------------------------------------------------------------------


def test_a_stranger_gets_404(as_owner, page, stranger):
    response = patch(
        as_owner,
        page,
        {"title": "Mine now"},
        user_id=str(stranger["account"]["id"]),
    )

    # Never 403. This API does not confirm that somebody else's memoir exists.
    assert response.status_code == 404


def test_a_link_alone_cannot_edit_a_page(client, page):
    response = client.patch(
        f"/chapters/{page['chapter_id']}",
        json={"title": "Mine now"},
        headers={"X-Link-Token": "0" * 48},
    )

    # Bearer only, unlike the GET beside it: a share token is permission to
    # read a memoir, never to change what it says.
    assert response.status_code == 401


def test_a_sealed_memoir_refuses_every_edit(as_owner, page, factory):
    factory.publish(page["memoir_id"])

    assert patch(as_owner, page, {"title": "Too late"}).status_code == 409
    assert (
        patch(as_owner, page, {"blocks": [{"id": page["first"]}]}).status_code == 409
    )


def test_the_comment_layer_follows_an_edited_passage(as_owner, page):
    """A comment is anchored the same way a source is, and moves the same way.

    Normally there are none at this point — reading needs a link and a link
    needs publication — but the owner can comment on their own draft, and a
    comment left pointing at moved words is the exact failure the offsets were
    checked to avoid.
    """
    left = as_owner(page["owner_id"]).post(
        f"/chapters/{page['chapter_id']}/comments",
        json={
            "block_id": page["first"],
            "start_offset": page["text"].index(page["clause"]),
            "end_offset": len(page["text"]),
            "body": "This is the bit I want to keep.",
        },
    )
    assert left.status_code == 201, left.text

    rewritten = "It was poor. " + page["clause"]
    patch(
        as_owner,
        page,
        {
            "blocks": [
                {"id": page["first"], "text": rewritten},
                {"id": page["second"]},
                {"id": page["figure"]},
            ]
        },
    )

    read = as_owner(page["owner_id"]).get(f"/chapters/{page['chapter_id']}")
    thread = read.json()["threads"][0]
    assert thread["start_offset"] == rewritten.index(page["clause"])
