"""The plan document, as pure functions. No database, no HTTP.

Three things are tested here, and the first is the one that matters:

  `resurvey`     what happens to a source's character span when the owner
                 edits the passage it points into
  `rehydrate`    what happens to a stored plan when the archive moves on
  `_merge`       what an edit may and may not change

`resurvey` is the load-bearing one. `block_source.start_offset`/`end_offset`
are character positions into block text, and migration 0011 is explicit that
they are only safe because publication is immutable. Editing a plan before
assembly is the one moment that text legitimately moves, so this is the
function that decides whether a family's attribution survives it. A stale
offset is not a display bug: it credits one person's sentence to another,
permanently, in a document nobody can correct afterwards.
"""

import pytest

from src.domain.chapters import plan_service


def _source(start=None, end=None, memory_id="m1", diverges=False):
    return {
        "memory_id": memory_id,
        "start_offset": start,
        "end_offset": end,
        "diverges": diverges,
    }


# ---------------------------------------------------------------------------
# resurvey
# ---------------------------------------------------------------------------


def test_unedited_text_keeps_every_span_untouched():
    """The common case. Renaming a chapter does not touch a character of prose."""
    text = "He counted the ripple rings behind our canoe."
    sources = [_source(3, 10), _source(None, None, memory_id="m2")]

    assert plan_service.resurvey(text, text, sources) is sources


def test_a_span_moves_with_its_words():
    """An edit before the span shifts it, and the span still covers its words.

    The whole point of re-finding rather than adjusting: nobody has to reason
    about how many characters were inserted where.
    """
    old = "He counted the ripple rings behind our canoe."
    new = "That afternoon he counted the ripple rings behind our canoe."
    # "the ripple rings"
    sources = [_source(old.index("the ripple rings"), old.index("the ripple rings") + 16)]

    [moved] = plan_service.resurvey(old, new, sources)

    assert new[moved["start_offset"] : moved["end_offset"]] == "the ripple rings"


def test_a_span_whose_words_were_deleted_falls_back_to_the_whole_block():
    """Offsets go NULL rather than pointing at whatever is now in their place.

    A wrong offset is worse than no offset. Whole-block attribution still says
    the true thing — this person is a source for this passage — and loses only
    the precision of which clause.
    """
    old = "The cedar planks had soaked in July heat all day long."
    new = "The cedar planks had soaked all day long."
    sources = [_source(old.index("in July heat"), old.index("in July heat") + 12)]

    [lost] = plan_service.resurvey(old, new, sources)

    assert lost["start_offset"] is None
    assert lost["end_offset"] is None
    # The credit itself is untouched. Only the span is gone.
    assert lost["memory_id"] == "m1"


def test_an_ambiguous_span_falls_back_too():
    """If the words now appear twice, there is no way to know which was meant.

    Same rule `planner.verify` applies to a quote that appears twice, and for
    the same reason: guessing between two would be right half the time.
    """
    old = "the house was cold"
    new = "the house was cold, and the house was cold again"
    sources = [_source(0, len("the house was cold"))]

    [ambiguous] = plan_service.resurvey(old, new, sources)

    assert ambiguous["start_offset"] is None


def test_a_whole_block_source_stays_whole():
    """There is nothing to lose, so nothing is done."""
    sources = [_source(None, None)]

    [same] = plan_service.resurvey("before", "after", sources)

    assert same["start_offset"] is None
    assert same["diverges"] is False


def test_resurvey_does_not_mutate_what_it_was_given():
    """The caller still holds the stored plan, and may need it unchanged."""
    old = "one two three"
    sources = [_source(4, 7)]

    plan_service.resurvey(old, "zero one two three", sources)

    assert sources[0]["start_offset"] == 4


# ---------------------------------------------------------------------------
# rehydrate
# ---------------------------------------------------------------------------


def _memory(memory_id, participant="p1"):
    return {"id": memory_id, "participant_id": participant}


def _document(**overrides):
    chapter = {
        "id": "c1",
        "title": "Summers at the lake",
        "from_year": 1982,
        "through_year": 1984,
        "blocks": [
            {
                "kind": "paragraph",
                "text": "The cedar planks had soaked in July heat.",
                "sources": [_source(None, None, memory_id="m1")],
            }
        ],
        "figures": [
            {"asset_id": "a1", "anchor_memory_id": "m1", "placement": "inset"}
        ],
        "memory_ids": ["m1"],
    }
    chapter.update(overrides)
    return {"chapters": [chapter]}


def test_rehydrate_joins_the_plan_back_to_the_archive():
    """`participant_id` comes from the row, never from the document."""
    [chapter] = plan_service.rehydrate(
        _document(),
        [_memory("m1", participant="margaret")],
        [{"id": "a1"}],
    )

    assert chapter["title"] == "Summers at the lake"
    assert chapter["blocks"][0]["sources"][0]["participant_id"] == "margaret"
    assert [m["id"] for m in chapter["memories"]] == ["m1"]


def test_a_deleted_memory_takes_its_passage_with_it():
    """The foreign key would refuse it; this refuses it first, and says so.

    A block left with no source is dropped, and a chapter left with no block
    is dropped, which is the same rule an unattributed paragraph gets
    everywhere else in this product.
    """
    assert plan_service.rehydrate(_document(), [], [{"id": "a1"}]) == []


def test_a_deleted_photograph_drops_out_but_the_chapter_stays():
    """One missing picture is not a missing chapter."""
    [chapter] = plan_service.rehydrate(_document(), [_memory("m1")], [])

    assert chapter["figures"] == []
    assert len(chapter["blocks"]) == 1


def test_a_figure_anchored_to_a_vanished_memory_is_dropped():
    """Otherwise `_write_chapter` falls back to the last paragraph in the
    chapter and puts the photograph beside prose it has nothing to do with."""
    document = _document(
        blocks=[
            {
                "kind": "paragraph",
                "text": "He counted the ripple rings.",
                "sources": [_source(None, None, memory_id="m2")],
            }
        ],
        memory_ids=["m1", "m2"],
    )

    [chapter] = plan_service.rehydrate(
        document, [_memory("m2")], [{"id": "a1"}]
    )

    # The anchor memory m1 is gone from the archive, so its figure goes too.
    assert chapter["figures"] == []


# ---------------------------------------------------------------------------
# _merge — what an edit may change
# ---------------------------------------------------------------------------


def _stored():
    return {
        "id": "c1",
        "title": "Summers at the lake",
        "from_year": 1982,
        "through_year": 1984,
        "blocks": [
            {
                "kind": "paragraph",
                "text": "The cedar planks had soaked in July heat.",
                "sources": [_source(4, 16, memory_id="m1")],
            },
            {
                "kind": "paragraph",
                "text": "He counted the ripple rings.",
                "sources": [_source(None, None, memory_id="m2")],
            },
        ],
        "figures": [
            {"asset_id": "a1", "anchor_memory_id": "m1", "placement": "margin"}
        ],
        "memory_ids": ["m1", "m2"],
    }


def _submitted(**overrides):
    body = {
        "id": "c1",
        "title": "Summers at the lake",
        "blocks": [
            {"index": 0, "text": "The cedar planks had soaked in July heat."},
            {"index": 1, "text": "He counted the ripple rings."},
        ],
        "figures": [
            {"asset_id": "a1", "anchor_memory_id": "m1", "placement": "margin"}
        ],
    }
    body.update(overrides)
    return body


def test_a_renamed_chapter_keeps_everything_else():
    merged = plan_service._merge(_stored(), _submitted(title="The dock"))

    assert merged["title"] == "The dock"
    assert len(merged["blocks"]) == 2
    assert merged["from_year"] == 1982


def test_a_blank_title_falls_back_rather_than_losing_the_chapter():
    """`chapter_title_not_blank` refuses empty, and a stray backspace should
    not cost a chapter."""
    merged = plan_service._merge(_stored(), _submitted(title="   "))

    assert merged["title"] == "Summers at the lake"


def test_passages_can_be_reordered():
    """Array position is the order. Nothing trusts an ordinal from a client."""
    reversed_blocks = [
        {"index": 1, "text": "He counted the ripple rings."},
        {"index": 0, "text": "The cedar planks had soaked in July heat."},
    ]

    merged = plan_service._merge(_stored(), _submitted(blocks=reversed_blocks))

    assert merged["blocks"][0]["text"] == "He counted the ripple rings."


def test_a_dropped_passage_takes_its_photographs_with_it():
    """`memory_ids` is recomputed from what the surviving passages cite.

    So dropping the passage that quoted m1 also drops the figure anchored to
    m1 — the photograph has nothing left to sit beside in this chapter.
    """
    merged = plan_service._merge(
        _stored(),
        _submitted(blocks=[{"index": 1, "text": "He counted the ripple rings."}]),
    )

    assert merged["memory_ids"] == ["m2"]
    assert merged["figures"] == []


def test_an_edit_cannot_reassign_attribution():
    """The sources are read from the stored plan, never from the request.

    The strongest guarantee in this file. A client that could name its own
    sources could credit anybody's sentence to anybody's grandmother, in a
    document that becomes immutable.
    """
    merged = plan_service._merge(
        _stored(),
        _submitted(
            blocks=[
                {
                    "index": 0,
                    "text": "The cedar planks had soaked in July heat.",
                    "sources": [_source(0, 3, memory_id="somebody-else")],
                }
            ]
        ),
    )

    assert [s["memory_id"] for s in merged["blocks"][0]["sources"]] == ["m1"]


def test_editing_a_passage_resurveys_its_spans():
    """The two halves of the feature, together: text changes, spans follow."""
    merged = plan_service._merge(
        _stored(),
        _submitted(
            blocks=[
                {
                    "index": 0,
                    "text": "That July the cedar planks had soaked in July heat.",
                }
            ]
        ),
    )

    source = merged["blocks"][0]["sources"][0]
    # "cedar planks" appeared once before and once after; "July" now appears
    # twice, so an implementation that searched for the wrong thing would go
    # ambiguous here. This span was "cedar planks".
    text = merged["blocks"][0]["text"]
    assert text[source["start_offset"] : source["end_offset"]] == "cedar planks"


def test_a_photograph_can_be_moved_to_a_carousel():
    merged = plan_service._merge(
        _stored(),
        _submitted(
            figures=[
                {
                    "asset_id": "a1",
                    "anchor_memory_id": "m2",
                    "placement": "carousel",
                }
            ]
        ),
    )

    assert merged["figures"] == [
        {"asset_id": "a1", "anchor_memory_id": "m2", "placement": "carousel"}
    ]


def test_a_photograph_the_plan_never_held_cannot_be_added():
    """Only a picture the model actually placed can be moved."""
    merged = plan_service._merge(
        _stored(),
        _submitted(
            figures=[
                {
                    "asset_id": "not-in-the-plan",
                    "anchor_memory_id": "m1",
                    "placement": "inset",
                }
            ]
        ),
    )

    assert merged["figures"] == []


def test_a_passage_that_is_not_in_the_chapter_is_ignored():
    """A stale screen naming a passage index that no longer exists."""
    merged = plan_service._merge(
        _stored(), _submitted(blocks=[{"index": 9, "text": "invented"}])
    )

    assert merged is None


def test_an_emptied_chapter_is_none():
    """Dropping every passage drops the chapter. `edit` turns this into a 409
    when it happens to all of them."""
    assert plan_service._merge(_stored(), _submitted(blocks=[])) is None


# ---------------------------------------------------------------------------
# store refuses an origin it does not recognise
# ---------------------------------------------------------------------------


def test_store_refuses_an_unknown_origin():
    """Mirrors the CHECK constraint, and fails in Python where the message is
    legible rather than as a 23514 from Postgres."""
    with pytest.raises(ValueError, match="unknown plan origin"):
        # No cursor is needed: the guard runs before any SQL.
        import asyncio

        asyncio.run(plan_service.store(None, "memoir", [], "vibes"))
