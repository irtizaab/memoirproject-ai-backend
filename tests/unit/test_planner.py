# The one piece of assembly that decides whether "never fabricate" is true.
#
# `planner.verify` turns what a model claimed into what the database is allowed
# to store. Everything it gets wrong is permanent and invisible: publication is
# immutable, so a `block_source` row pointing at the wrong clause credits one
# person's sentence to another person's grandmother forever, and no reader can
# tell because the credit line looks exactly like a correct one.
#
# No network, no key, no database.

from src.domain.chapters.planner import (
    Finding,
    Plan,
    PlannedBlock,
    PlannedChapter,
    PlannedSource,
    Review,
    _accept_revision,
    _clean,
    _findings,
    _shape,
    verify,
)

# Two memories that exist, and the people who left them. This is the archive
# the assembler actually read — the only authority on what may be cited.
ALLOWED = {"mem-a": "person-1", "mem-b": "person-2"}

TEXT = "The back door stuck in summer. Margaret only ever used the front."


def test_a_quote_becomes_real_offsets():
    """The normal case: a clause located in the block it claims to be in."""
    quote = "Margaret only ever used the front."

    [source] = verify(TEXT, [PlannedSource(memory_id="mem-a", quote=quote)], ALLOWED)

    assert source["start_offset"] == TEXT.index(quote)
    assert source["end_offset"] == len(TEXT)
    # And the offsets point at what they claim to, which is the whole point.
    assert TEXT[source["start_offset"] : source["end_offset"]] == quote


def test_a_null_quote_is_whole_block_attribution():
    """No quote means the block is one person's words. NULL offsets say so."""
    [source] = verify(TEXT, [PlannedSource(memory_id="mem-a")], ALLOWED)

    assert source["start_offset"] is None
    assert source["end_offset"] is None


def test_a_quote_that_is_not_there_falls_back_rather_than_guessing():
    """A model quoting the source memory instead of its own paragraph.

    The credit is still right — that memory did go into this block — so it is
    kept. Only the precision is lost. Inventing an offset would be worse than
    having none.
    """
    [source] = verify(
        TEXT,
        [PlannedSource(memory_id="mem-a", quote="the scullery door")],
        ALLOWED,
    )

    assert source["memory_id"] == "mem-a"
    assert source["start_offset"] is None
    assert source["end_offset"] is None


def test_an_ambiguous_quote_falls_back_too():
    """"door" appears twice. There is no way to know which was meant."""
    text = "The back door stuck. The front door did not."

    [source] = verify(text, [PlannedSource(memory_id="mem-a", quote="door")], ALLOWED)

    assert source["start_offset"] is None


def test_an_unknown_memory_is_dropped():
    """A hallucinated id never reaches `block_source`.

    The foreign key would refuse it anyway — but as a psycopg error in the
    middle of a transaction that was writing somebody's book, rather than here.
    """
    assert verify(TEXT, [PlannedSource(memory_id="mem-zzz")], ALLOWED) == []


def test_the_participant_comes_from_the_archive_not_the_model():
    """Attribution is read from the row. The model is not asked and not believed."""
    [source] = verify(TEXT, [PlannedSource(memory_id="mem-b")], ALLOWED)
    assert source["participant_id"] == "person-2"


def test_one_credit_per_person_per_block():
    """A memory cited twice collapses. The reader prints one credit line."""
    sources = verify(
        TEXT,
        [
            PlannedSource(memory_id="mem-a", quote="The back door stuck in summer."),
            PlannedSource(memory_id="mem-a", quote="Margaret only ever used the front."),
        ],
        ALLOWED,
    )

    assert len(sources) == 1


def test_diverges_survives():
    """The divergent-accounts rule is stored, not argued about."""
    [source] = verify(
        TEXT, [PlannedSource(memory_id="mem-a", diverges=True)], ALLOWED
    )
    assert source["diverges"] is True


# ---------------------------------------------------------------------------
# What a whole plan is reduced to
# ---------------------------------------------------------------------------


def _memories():
    """The archive `_clean` checks a plan against."""
    return [
        {"id": "mem-a", "participant_id": "person-1"},
        {"id": "mem-b", "participant_id": "person-2"},
    ]


def test_a_block_with_no_valid_source_is_dropped():
    """An unattributed paragraph in this product is a fabricated one."""
    plan = Plan(
        chapters=[
            PlannedChapter(
                title="The house",
                blocks=[
                    PlannedBlock(text="Somebody said this.", sources=[]),
                    PlannedBlock(
                        text=TEXT, sources=[PlannedSource(memory_id="mem-a")]
                    ),
                ],
            )
        ]
    )

    [chapter], _ = _clean(plan, _memories())

    assert len(chapter["blocks"]) == 1
    assert chapter["blocks"][0]["text"] == TEXT


def test_a_chapter_left_with_nothing_is_dropped():
    """`chapter_title_not_blank` would otherwise leave an empty page."""
    plan = Plan(
        chapters=[
            PlannedChapter(
                title="Nothing survived",
                blocks=[PlannedBlock(text="Unsourced.", sources=[])],
            )
        ]
    )

    assert _clean(plan, _memories())[0] == []


def test_an_unknown_block_kind_becomes_a_paragraph():
    """The enum would refuse it, and a paragraph is what it almost certainly is."""
    plan = Plan(
        chapters=[
            PlannedChapter(
                title="The house",
                blocks=[
                    PlannedBlock(
                        kind="epigraph",
                        text=TEXT,
                        sources=[PlannedSource(memory_id="mem-a")],
                    )
                ],
            )
        ]
    )

    [chapter], _ = _clean(plan, _memories())
    assert chapter["blocks"][0]["kind"] == "paragraph"


def test_only_cited_memories_are_carried_to_the_figure_pass():
    """A photograph belongs in the chapter that used the memory it came with."""
    plan = Plan(
        chapters=[
            PlannedChapter(
                title="The house",
                blocks=[
                    PlannedBlock(text=TEXT, sources=[PlannedSource(memory_id="mem-b")])
                ],
            )
        ]
    )

    [chapter], _ = _clean(plan, _memories())

    assert [m["id"] for m in chapter["memories"]] == ["mem-b"]


# ---------------------------------------------------------------------------
# What the reviewer is allowed to change
# ---------------------------------------------------------------------------


def _chapter():
    return {"title": "The house", "blocks": [{"kind": "paragraph", "text": TEXT}]}


def test_a_revision_that_loses_attribution_is_refused():
    """A re-worded paragraph can lose the quote that attributed it.

    The reviewer is asked to remove and re-word, never to add — but a model
    that drops a source while re-wording has made the book worse in the one
    way this product cannot tolerate. The measure is the tally: no more
    unattributed blocks than the draft had.
    """
    draft = {"blocks": 3, "unattributed": 0}
    worse = {"blocks": 3, "unattributed": 1}
    assert _accept_revision(draft, [_chapter()], worse) is False


def test_a_revision_with_no_chapters_is_refused():
    """Removing everything is not a correction."""
    assert _accept_revision({"unattributed": 2}, [], {"unattributed": 0}) is False


def test_a_revision_that_attributes_as_well_is_taken():
    draft = {"unattributed": 1}
    assert _accept_revision(draft, [_chapter()], {"unattributed": 1}) is True
    assert _accept_revision(draft, [_chapter()], {"unattributed": 0}) is True


def test_a_finding_is_not_fixed_by_a_revision_that_was_not_taken():
    """`fixed` is a claim about the revised plan. No revision, no fix."""
    review = Review(
        findings=[
            Finding(kind="attribution", note="A date nobody wrote.", fixed=True),
            Finding(kind="made-up", chapter="  ", note="  ", fixed=True),
        ]
    )

    stored = _findings(review, revised=False)

    assert stored == {
        "revised": False,
        "findings": [
            {
                "kind": "attribution",
                "chapter": None,
                "note": "A date nobody wrote.",
                "fixed": False,
            }
        ],
    }


def test_an_unknown_finding_kind_becomes_structure():
    """The frontend groups by a closed set; the model does not always use it."""
    review = Review(findings=[Finding(kind="vibes", note="Odd ordering.")])
    assert _findings(review, revised=True)["findings"][0]["kind"] == "structure"


# ---------------------------------------------------------------------------
# What the guide is allowed to know
# ---------------------------------------------------------------------------


def test_the_guide_sees_counts_and_never_words():
    """The guide talks to the owner and has read nothing a family wrote.

    Everything it is told about the archive comes through `_shape`, so this
    is the whole surface: if a memory's text ever appears here, a third model
    call has been handed prose it has no need of and nothing to check it
    against.
    """
    memories = [
        {"id": "a", "participant_id": "p1", "happened_on": "1961-06-01",
         "body_text": "The back door stuck in summer.", "title": "The house"},
        {"id": "b", "participant_id": "p2", "happened_on": None,
         "body_text": "Margaret only ever used the front.", "title": None},
    ]

    shape = _shape(memories, {"subject_name": "Margaret"})

    assert shape == {
        "subject": "Margaret",
        "memories": 2,
        "dated": 1,
        "earliest": "1961-06-01",
        "latest": "1961-06-01",
        "contributors": 2,
    }
    assert "back door" not in str(shape)
