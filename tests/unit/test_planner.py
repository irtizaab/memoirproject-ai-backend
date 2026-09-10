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
    Plan,
    PlannedBlock,
    PlannedChapter,
    PlannedSource,
    _clean,
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

    [chapter] = _clean(plan, _memories())

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

    assert _clean(plan, _memories()) == []


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

    [chapter] = _clean(plan, _memories())
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

    [chapter] = _clean(plan, _memories())

    assert [m["id"] for m in chapter["memories"]] == ["mem-b"]
