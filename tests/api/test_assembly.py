"""Assembling an archive into a book.

The deterministic assembler, which groups memories by decade and gives each one
its own paragraph. It is a placeholder for the Claude call in one sense only —
what it decides. Everything it *writes* is the real thing: real chapters, real
blocks, real attribution, read back through the same endpoints the family use.

So the tests worth having here are not about the grouping rule, which will be
replaced. They are about the promises that must survive replacing it:

  - every paragraph is somebody's own words, credited to them
  - nothing is invented, including years nobody supplied
  - a published memoir is never rewritten
  - somebody else's memoir is never touched
"""

import uuid

import pytest

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.db]


@pytest.fixture
def archive(factory, owner):
    """A memoir with four memories across two decades, one of them undated.

    Two contributors rather than one, because attribution is the thing under
    test and a single-contributor archive would let a broken join look right.
    """
    memoir_id = str(owner["memoir"]["id"])

    margaret = factory.contributor(memoir_id, display_name="Margaret Reyes")
    thomas = factory.contributor(memoir_id, display_name="Thomas Marsh")

    early = factory.memory(
        memoir_id,
        margaret["id"],
        kind="text",
        title=None,
        body_text="The cedar planks had soaked in July heat all day long.",
        happened_on="1982-07-14",
    )
    also_early = factory.memory(
        memoir_id,
        thomas["id"],
        kind="text",
        title=None,
        body_text="He counted the ripple rings behind our canoe.",
        happened_on="1984-08-02",
    )
    later = factory.memory(
        memoir_id,
        margaret["id"],
        kind="text",
        title=None,
        body_text="The boat stayed docked through twenty-four winters.",
        happened_on="1996-01-09",
    )
    undated = factory.memory(
        memoir_id,
        thomas["id"],
        kind="text",
        title=None,
        body_text="Nobody could say which summer this one was.",
        happened_on=None,
    )

    return {
        "memoir_id": memoir_id,
        "owner_id": str(owner["account"]["id"]),
        "margaret": margaret,
        "thomas": thomas,
        "memories": {
            "early": early,
            "also_early": also_early,
            "later": later,
            "undated": undated,
        },
    }


# ---------------------------------------------------------------------------
# Making a book
# ---------------------------------------------------------------------------


def test_an_archive_becomes_a_book(as_owner, archive):
    """The whole point: memories in, chapters out, counted honestly."""
    response = as_owner(archive["owner_id"]).post(
        f"/memoirs/{archive['memoir_id']}/assemble"
    )

    assert response.status_code == 200
    result = response.json()

    # Two dated decades and one undated chapter.
    assert result["chapters"] == 3
    # One paragraph per memory, and one credit per paragraph.
    assert result["blocks"] == 4
    assert result["sources"] == 4
    assert result["figures"] == 0


def test_chapters_come_back_in_the_order_they_were_lived(as_owner, archive):
    """Ordinal is reading order, and the undated chapter comes last.

    A memory nobody could date is not evidence that it happened last — it is
    evidence that nobody was asked — so it does not get sorted among the years.
    """
    client = as_owner(archive["owner_id"])
    client.post(f"/memoirs/{archive['memoir_id']}/assemble")

    reading = client.get(f"/memoirs/{archive['memoir_id']}/chapters").json()
    titles = [chapter["title"] for chapter in reading["chapters"]]

    assert titles == ["1982 – 1984", "1996", "Without a date"]
    assert [c["ordinal"] for c in reading["chapters"]] == [0, 1, 2]


def test_a_chapter_only_claims_years_somebody_supplied(as_owner, archive):
    """`from_year` and `through_year` are the real dates, not a guessed span.

    The decade band is how memories are *grouped*; it is not what the chapter
    says about itself. A chapter holding 1982 and 1984 covers 1982–1984, not
    1982–1991.
    """
    client = as_owner(archive["owner_id"])
    client.post(f"/memoirs/{archive['memoir_id']}/assemble")

    reading = client.get(f"/memoirs/{archive['memoir_id']}/chapters").json()
    first, second, undated = reading["chapters"]

    assert (first["from_year"], first["through_year"]) == (1982, 1984)
    assert (second["from_year"], second["through_year"]) == (1996, 1996)
    assert undated["from_year"] is None
    assert undated["through_year"] is None


def test_a_paragraph_is_the_contributors_own_words(as_owner, archive):
    """Nothing is rephrased, so nothing can be misattributed.

    The strongest guarantee this assembler makes, and the reason its blocks are
    credited whole: the text of a paragraph is one field of one memory, copied.
    """
    client = as_owner(archive["owner_id"])
    client.post(f"/memoirs/{archive['memoir_id']}/assemble")

    reading = client.get(f"/memoirs/{archive['memoir_id']}/chapters").json()
    chapter_id = reading["chapters"][0]["id"]
    chapter = client.get(f"/chapters/{chapter_id}").json()

    texts = [block["text"] for block in chapter["blocks"]]
    assert "The cedar planks had soaked in July heat all day long." in texts


def test_every_paragraph_carries_the_person_who_said_it(as_owner, archive):
    """A block with no source is a sentence nobody said."""
    client = as_owner(archive["owner_id"])
    client.post(f"/memoirs/{archive['memoir_id']}/assemble")

    reading = client.get(f"/memoirs/{archive['memoir_id']}/chapters").json()
    chapter = client.get(f"/chapters/{reading['chapters'][0]['id']}").json()

    for block in chapter["blocks"]:
        if block["kind"] == "paragraph":
            assert block["sources"], "a paragraph with no attribution"
            source = block["sources"][0]
            assert source["name"] in {"Margaret Reyes", "Thomas Marsh"}
            # Whole-block attribution: the paragraph IS their words, so there
            # is no range inside it that came from anywhere else.
            assert source["start_offset"] is None
            assert source["end_offset"] is None


def test_a_recording_speaks_through_its_transcript(as_owner, factory, owner):
    """A voice memory with no typed body contributes what was said.

    The transcript is that person's words as much as anything typed is, and a
    memoir that dropped every recording would be missing the material the
    product spends money transcribing.
    """
    memoir_id = str(owner["memoir"]["id"])
    speaker = factory.contributor(memoir_id, display_name="Ana Whitfield")
    memory = factory.memory(
        memoir_id,
        speaker["id"],
        kind="voice",
        title=None,
        body_text=None,
        happened_on="1991-03-02",
    )
    asset = factory.asset(
        memoir_id, memory_id=memory["id"], kind="audio", duration_ms=47000
    )
    factory.transcript(asset["id"], text="She was not practising. She was talking to it.")

    client = as_owner(str(owner["account"]["id"]))
    client.post(f"/memoirs/{memoir_id}/assemble")

    reading = client.get(f"/memoirs/{memoir_id}/chapters").json()
    chapter = client.get(f"/chapters/{reading['chapters'][0]['id']}").json()

    assert chapter["blocks"][0]["text"] == (
        "She was not practising. She was talking to it."
    )


def test_an_unfinished_recording_says_nothing(as_owner, factory, owner):
    """A queued transcript is not text yet, and an empty paragraph is not a memory.

    The memory stays in the archive. It simply has nothing a chapter can hold
    until the transcript comes back.
    """
    memoir_id = str(owner["memoir"]["id"])
    speaker = factory.contributor(memoir_id)
    memory = factory.memory(
        memoir_id, speaker["id"], kind="voice", title=None, body_text=None
    )
    asset = factory.asset(
        memoir_id, memory_id=memory["id"], kind="audio", duration_ms=8000
    )
    factory.transcript(asset["id"], status="queued", text=None)

    response = as_owner(str(owner["account"]["id"])).post(
        f"/memoirs/{memoir_id}/assemble"
    )

    assert response.status_code == 400
    assert "no memories" in response.json()["detail"]


def test_a_photograph_sits_beside_a_paragraph(as_owner, factory, owner):
    """A figure needs an anchor, and the schema will not store one without it."""
    memoir_id = str(owner["memoir"]["id"])
    giver = factory.contributor(memoir_id, display_name="Thomas Marsh")

    factory.memory(
        memoir_id,
        giver["id"],
        kind="text",
        title=None,
        body_text="The cottage, the summer the dock was rebuilt.",
        happened_on="1982-06-01",
    )
    photo = factory.memory(
        memoir_id,
        giver["id"],
        kind="photo",
        title=None,
        body_text="The original Lake George cottage.",
        happened_on="1982-07-01",
    )
    factory.asset(memoir_id, memory_id=photo["id"], kind="image")

    client = as_owner(str(owner["account"]["id"]))
    result = client.post(f"/memoirs/{memoir_id}/assemble").json()

    assert result["figures"] == 1

    reading = client.get(f"/memoirs/{memoir_id}/chapters").json()
    chapter = client.get(f"/chapters/{reading['chapters'][0]['id']}").json()
    figures = [b for b in chapter["blocks"] if b["kind"] == "figure"]

    assert len(figures) == 1
    assert figures[0]["figure"]["placement"] == "inset"


def test_a_photograph_with_nothing_to_sit_beside_is_left_out(
    as_owner, factory, owner
):
    """Not lost — still in the archive, just not placed.

    The alternative is anchoring it to a paragraph from a different part of the
    life, which would caption it with somebody else's afternoon.
    """
    memoir_id = str(owner["memoir"]["id"])
    giver = factory.contributor(memoir_id)

    # Prose in one decade, a captionless photograph in another.
    factory.memory(
        memoir_id,
        giver["id"],
        kind="text",
        title=None,
        body_text="The only writing in this archive.",
        happened_on="1970-01-01",
    )
    photo = factory.memory(
        memoir_id,
        giver["id"],
        kind="photo",
        title=None,
        body_text=None,
        happened_on="1999-01-01",
    )
    factory.asset(memoir_id, memory_id=photo["id"], kind="image")

    result = as_owner(str(owner["account"]["id"])).post(
        f"/memoirs/{memoir_id}/assemble"
    ).json()

    assert result["chapters"] == 1
    assert result["figures"] == 0


# ---------------------------------------------------------------------------
# Making it again
# ---------------------------------------------------------------------------


def test_assembling_twice_rebuilds_rather_than_doubles(as_owner, archive):
    """The owner adds a memory and runs it again. They get a book, not two."""
    client = as_owner(archive["owner_id"])

    first = client.post(f"/memoirs/{archive['memoir_id']}/assemble").json()
    second = client.post(f"/memoirs/{archive['memoir_id']}/assemble").json()

    assert first == second


def test_a_published_memoir_is_never_reassembled(as_owner, factory, archive):
    """The immutability rule, enforced where it would otherwise be broken.

    Comments and sources are anchored to character offsets in block text.
    Rewriting the blocks under a published memoir would leave every one of them
    pointing at words that had moved — silently, and forever.
    """
    factory.publish(archive["memoir_id"])

    response = as_owner(archive["owner_id"]).post(
        f"/memoirs/{archive['memoir_id']}/assemble"
    )

    assert response.status_code == 409
    assert "published" in response.json()["detail"]


def test_an_empty_archive_is_not_a_missing_memoir(as_owner, owner):
    """400 and a sentence, not a 404.

    Telling somebody their memoir does not exist because they have not added a
    memory yet would be a lie about the thing they care most about.
    """
    response = as_owner(str(owner["account"]["id"])).post(
        f"/memoirs/{owner['memoir']['id']}/assemble"
    )

    assert response.status_code == 400
    assert "no memories" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Whose memoir it is
# ---------------------------------------------------------------------------


def test_a_stranger_cannot_assemble_someone_elses_memoir(as_owner, stranger, archive):
    """404, not 403 — this API never confirms a stranger's memoir exists."""
    response = as_owner(str(stranger["account"]["id"])).post(
        f"/memoirs/{archive['memoir_id']}/assemble"
    )

    assert response.status_code == 404


def test_a_memoir_that_does_not_exist_answers_the_same_way(as_owner, archive):
    """Unknown and not-yours are one response."""
    response = as_owner(archive["owner_id"]).post(
        f"/memoirs/{uuid.uuid4()}/assemble"
    )

    assert response.status_code == 404


def test_assembly_touches_nothing_outside_its_own_memoir(
    as_owner, factory, stranger, archive
):
    """A rebuild deletes chapters. It must delete only this memoir's.

    The DELETE is filtered on memoir_id, and this is the test that says so — a
    missing WHERE clause here would take another family's book with it.
    """
    theirs = str(stranger["memoir"]["id"])
    factory.chapter(theirs, ordinal=0, title="Their chapter")

    as_owner(archive["owner_id"]).post(f"/memoirs/{archive['memoir_id']}/assemble")

    reading = as_owner(str(stranger["account"]["id"])).get(
        f"/memoirs/{theirs}/chapters"
    ).json()

    assert [c["title"] for c in reading["chapters"]] == ["Their chapter"]


# ---------------------------------------------------------------------------
# What the dashboard reads
# ---------------------------------------------------------------------------


def test_the_account_says_whether_there_is_a_book_yet(as_owner, archive):
    """`chapter_count` on GET /me is what unlocks "view memoir" and the export.

    Zero before assembly and the real number after, because the dashboard shows
    nothing that opens the reader until there is something to open.
    """
    client = as_owner(archive["owner_id"])

    before = client.get("/me").json()
    assert before["memoirs"][0]["chapter_count"] == 0

    client.post(f"/memoirs/{archive['memoir_id']}/assemble")

    after = client.get("/me").json()
    assert after["memoirs"][0]["chapter_count"] == 3
