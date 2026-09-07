"""Finding one afternoon in a life.

A memoir is the kind of document where "I know it is in here somewhere" is the
normal way to arrive. These tests are about four things: that all four kinds of
material are reachable, that the owner and the family get the same answer, that
the counts on the filters agree with the results they filter, and that search
does not become a way around any of the walls elsewhere in the API.
"""

import pytest

from tests.conftest import requires_db
from tests.factories import TEST_PASSPHRASE

pytestmark = [requires_db, pytest.mark.db]

# The two control characters `ts_headline` wraps a match in. Not `<mark>`,
# because the excerpt is built from text a reader typed.
START, END = "\x02", "\x03"


@pytest.fixture
def searchable(factory, client, owner):
    """A memoir with something of every kind in it, all mentioning one place."""
    memoir_id = str(owner["memoir"]["id"])

    margaret = factory.contributor(memoir_id, display_name="Margaret Reyes")
    thomas = factory.contributor(memoir_id, display_name="Thomas Marsh")

    # A chapter, and a paragraph in it.
    chapter = factory.chapter(memoir_id, ordinal=0, title="The Wooden Dock at Twilight")
    block = factory.block(
        memoir_id,
        str(chapter["id"]),
        ordinal=0,
        text=(
            "The cedar planks of the dock had soaked in July heat all day, "
            "releasing their resin into the air above Lake George."
        ),
    )

    # A photograph, found through the caption its giver wrote.
    photo = factory.memory(
        memoir_id, thomas["id"], kind="photo",
        title="The Original Cabin",
        body_text="Captured from a canoe on Lake George just before sunset.",
        happened_on="1988-06-21",
    )
    factory.asset(memoir_id, memory_id=photo["id"], kind="image")

    # A recording, found through its transcript.
    spoken = factory.memory(
        memoir_id, margaret["id"], kind="voice", title="By the fire",
        body_text=None, happened_on="1994-08-01",
    )
    audio = factory.asset(
        memoir_id, memory_id=spoken["id"], kind="audio", duration_ms=47000
    )
    factory.transcript(
        audio["id"], text="You could hear a whisper from three shores away on Lake George."
    )

    # A reflection, left by a reader on the paragraph.
    view = factory.link(memoir_id, scope="view")
    factory.protect(memoir_id)
    session = client.post(
        f"/r/{view['token']}/open",
        json={
            "passphrase": TEST_PASSPHRASE,
            "display_name": "Clara Harrison",
            "relationship": "Granddaughter",
        },
    ).json()
    headers = {
        "X-Link-Token": view["token"],
        "X-Reader-Token": session["reader_token"],
    }
    client.post(
        f"/chapters/{chapter['id']}/comments",
        headers=headers,
        json={
            "block_id": str(block["id"]),
            "body": "Uncle Arthur's storytelling by the fire on the shore of Lake George.",
        },
    )

    return {
        "memoir_id": memoir_id,
        "owner_id": str(owner["account"]["id"]),
        "chapter_id": str(chapter["id"]),
        "view_token": view["token"],
        "headers": headers,
    }


def owner_search(as_owner, searchable, q):
    return as_owner(searchable["owner_id"]).get(
        f"/memoirs/{searchable['memoir_id']}/search", params={"q": q}
    )


def reader_search(client, searchable, q):
    return client.get(
        f"/r/{searchable['view_token']}/search",
        params={"q": q},
        headers=searchable["headers"],
    )


# ---------------------------------------------------------------------------
# What it finds
# ---------------------------------------------------------------------------


def test_one_phrase_finds_all_four_kinds(as_owner, searchable):
    """Prose, photograph, recording and reflection, in one answer.

    One question — "where is that story" — and the person asking does not know
    which of the four they are looking for.
    """
    results = owner_search(as_owner, searchable, "Lake George").json()

    assert results["total"] == 4
    assert results["counts"] == {
        "chapter": 1,
        "photo": 1,
        "recording": 1,
        "reflection": 1,
    }


def test_a_result_says_where_to_go(as_owner, searchable):
    """A paragraph carries its chapter, so a hit is a link to the page it is
    on rather than to the top of the book."""
    results = owner_search(as_owner, searchable, "cedar planks").json()
    hit = next(h for h in results["hits"] if h["kind"] == "chapter")

    assert hit["chapter_id"] == searchable["chapter_id"]
    assert hit["title"] == "The Wooden Dock at Twilight"


def test_the_match_is_marked_without_being_html(as_owner, searchable):
    """The excerpt is built from text a reader typed. Returning `<mark>` would
    mean the frontend rendering user input as markup."""
    results = owner_search(as_owner, searchable, "cedar").json()
    excerpt = results["hits"][0]["excerpt"]

    assert START in excerpt and END in excerpt
    assert "<" not in excerpt
    assert f"{START}cedar{END}" in excerpt


def test_a_photograph_is_found_by_the_caption_its_giver_wrote(as_owner, searchable):
    results = owner_search(as_owner, searchable, "canoe").json()
    hit = results["hits"][0]

    assert hit["kind"] == "photo"
    assert hit["attribution"] == "Thomas Marsh"
    assert hit["year"] == 1988


def test_a_recording_is_found_by_what_was_said(as_owner, searchable):
    results = owner_search(as_owner, searchable, "whisper").json()
    hit = results["hits"][0]

    assert hit["kind"] == "recording"
    assert hit["attribution"] == "Margaret Reyes"


def test_a_reflection_is_found_years_later(as_owner, searchable):
    """Otherwise the least findable material in the product."""
    results = owner_search(as_owner, searchable, "Uncle Arthur").json()
    hit = results["hits"][0]

    assert hit["kind"] == "reflection"
    assert hit["attribution"] == "Clara Harrison"
    assert hit["chapter_id"] == searchable["chapter_id"]


def test_search_understands_stemming(as_owner, searchable):
    """"soaking" finds "soaked". A substring match would not, and a family
    searching their own memories should not have to guess a tense."""
    assert owner_search(as_owner, searchable, "soaking").json()["total"] == 1


def test_a_quoted_phrase_holds_together(as_owner, searchable):
    """`websearch_to_tsquery`, so the box behaves like every other search box
    somebody has used."""
    loose = owner_search(as_owner, searchable, "shores whisper").json()
    quoted = owner_search(as_owner, searchable, '"three shores away"').json()

    assert loose["total"] >= 1
    assert quoted["total"] == 1


def test_nothing_matching_is_not_an_error(as_owner, searchable):
    results = owner_search(as_owner, searchable, "helicopter").json()

    assert results["total"] == 0
    assert results["hits"] == []
    assert results["counts"] == {}


def test_an_empty_query_returns_nothing_quietly(as_owner, searchable):
    """Somebody who has typed one character and paused has not made a mistake."""
    assert owner_search(as_owner, searchable, "   ").json()["total"] == 0


def test_the_counts_are_the_results(as_owner, searchable):
    """A filter that says 1 and shows 0 is the kind of thing that makes a
    family stop trusting a page."""
    results = owner_search(as_owner, searchable, "Lake George").json()

    counted: dict = {}
    for hit in results["hits"]:
        counted[hit["kind"]] = counted.get(hit["kind"], 0) + 1

    assert counted == results["counts"]
    assert sum(results["counts"].values()) == results["total"]


# ---------------------------------------------------------------------------
# Who may search
# ---------------------------------------------------------------------------


def test_the_family_and_the_owner_get_the_same_answer(as_owner, client, searchable):
    """A search that quietly returned less to the family would have them
    wondering what else was being kept from them."""
    mine = owner_search(as_owner, searchable, "Lake George").json()
    theirs = reader_search(client, searchable, "Lake George").json()

    assert theirs["total"] == mine["total"]
    assert theirs["counts"] == mine["counts"]
    assert {h["id"] for h in theirs["hits"]} == {h["id"] for h in mine["hits"]}


def test_a_link_without_a_session_searches_nothing(client, searchable):
    """A forwarded link must not become a way to search a family's memoir."""
    response = client.get(
        f"/r/{searchable['view_token']}/search", params={"q": "Lake George"}
    )
    assert response.status_code == 404


def test_a_stranger_cannot_search_someone_elses_memoir(as_owner, stranger, searchable):
    response = as_owner(str(stranger["account"]["id"])).get(
        f"/memoirs/{searchable['memoir_id']}/search", params={"q": "Lake George"}
    )
    assert response.status_code == 404


def test_search_stays_inside_one_memoir(as_owner, factory, stranger, searchable):
    """The filter that matters most in a table shared by every family."""
    theirs = str(stranger["memoir"]["id"])
    other_chapter = factory.chapter(theirs, ordinal=0, title="Somebody Else's Book")
    factory.block(
        theirs, str(other_chapter["id"]), ordinal=0,
        text="They also spent summers on Lake George.",
    )

    results = owner_search(as_owner, searchable, "Lake George").json()

    assert all(h["title"] != "Somebody Else's Book" for h in results["hits"])
    assert results["total"] == 4


def test_the_owners_private_answer_is_not_searchable(as_owner, owner, db_cursor):
    """`never_forget` is filtered out of every response in the API, and an
    index over it would be the one place it came back — as a search result, to
    whoever holds the link."""
    memoir_id = str(owner["memoir"]["id"])

    db_cursor.execute(
        "UPDATE memoir SET never_forget = %(t)s WHERE id = %(id)s",
        {"t": "He fed the whole street during the floods.", "id": memoir_id},
    )

    results = as_owner(str(owner["account"]["id"])).get(
        f"/memoirs/{memoir_id}/search", params={"q": "floods"}
    ).json()

    assert results["total"] == 0
