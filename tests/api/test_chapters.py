"""The finished memoir: reading it, and talking about it.

Two things are worth knowing before reading these.

**Nothing in the application writes a chapter.** Assembly is a later slice, so
every chapter here is built by the factory, the same way `publish()` builds a
state the API cannot reach. What is under test is the read path and the shape
it returns — the contract the assembly step will have to satisfy.

**Two credentials reach the same rows.** An owner's bearer token and a live
*view* link both open a chapter. Most tests below are written twice for that
reason, because "it works for the owner" has never implied "and it works for
the family", and the family are the ones the product is for.
"""

import uuid

import pytest

from tests.conftest import TOKEN_PATTERN, requires_db

pytestmark = [requires_db, pytest.mark.db]


# ---------------------------------------------------------------------------
# A small book, built once
# ---------------------------------------------------------------------------


@pytest.fixture
def book(factory, owner):
    """One memoir, one chapter, two paragraphs, a photograph and two sources.

    Deliberately not the smallest thing that passes. The reader's whole design
    rests on a paragraph being a *composite* — assembled from more than one
    person, with the words of each identified — so a fixture with one source
    per paragraph would let a broken join look correct.
    """
    memoir_id = str(owner["memoir"]["id"])

    margaret = factory.contributor(memoir_id, display_name="Margaret Reyes")
    claire = factory.contributor(memoir_id, display_name="Claire Donnelly")

    hers = factory.memory(
        memoir_id, margaret["id"], kind="voice", title=None, body_text="She was talking to it."
    )
    theirs = factory.memory(
        memoir_id, claire["id"], kind="text", title=None, body_text="She played worse."
    )

    chapter = factory.chapter(memoir_id, ordinal=0)
    chapter_id = str(chapter["id"])

    # "The Chickering was not a good instrument. Three of its hammers…"
    #  0                                       39
    first = factory.block(
        memoir_id,
        chapter_id,
        ordinal=0,
        text="The Chickering was not a good instrument. Three of its hammers had been re-felted.",
    )
    second = factory.block(
        memoir_id, chapter_id, ordinal=2, text="She came back to the keys in the spring."
    )

    # One paragraph, two people, and they disagree. Both are kept.
    factory.source(
        memoir_id,
        str(first["id"]),
        memory_id=str(hers["id"]),
        participant_id=str(margaret["id"]),
        start_offset=41,
        end_offset=81,
    )
    factory.source(
        memoir_id,
        str(first["id"]),
        memory_id=str(theirs["id"]),
        participant_id=str(claire["id"]),
        diverges=True,
    )
    factory.source(
        memoir_id,
        str(second["id"]),
        memory_id=str(hers["id"]),
        participant_id=str(margaret["id"]),
    )

    asset = factory.asset(memoir_id, memory_id=str(theirs["id"]), kind="image")
    figure = factory.figure(
        memoir_id,
        chapter_id,
        asset_id=str(asset["id"]),
        anchor_block_id=str(first["id"]),
        ordinal=1,
    )

    view = factory.link(memoir_id, scope="view")

    return {
        "memoir_id": memoir_id,
        "owner_id": str(owner["account"]["id"]),
        "chapter_id": chapter_id,
        "first_block": str(first["id"]),
        "second_block": str(second["id"]),
        "figure_id": str(figure["id"]),
        "asset_id": str(asset["id"]),
        "margaret": margaret,
        "claire": claire,
        "view_token": view["token"],
        "view_link_id": str(view["id"]),
        "contribute_token": owner["memoir"]["link_token"],
    }


def _link(token: str) -> dict:
    return {"X-Link-Token": token}


# ---------------------------------------------------------------------------
# The covers
# ---------------------------------------------------------------------------


def test_a_view_link_opens_the_book(client, book):
    """The reader's entry point, with no credential but the token."""
    response = client.get(f"/r/{book['view_token']}")
    assert response.status_code == 200

    reading = response.json()
    assert reading["subject_name"] == "Nusrat Bibi"
    assert [c["title"] for c in reading["chapters"]] == [
        "The House on Ellsworth Lane"
    ]
    assert reading["totals"]["chapters"] == 1
    assert reading["totals"]["memories"] == 2
    assert reading["totals"]["people"] == 2


def test_the_book_never_carries_the_owners_private_answer(factory, client):
    """`never_forget` is the owner's answer, and a view link gets forwarded.

    The same reasoning that keeps it off `GET /j/{token}`. `response_model` is
    what enforces it, so this test is really asserting that nobody widened
    `MemoirReading` in passing.
    """
    account = factory.account()
    memoir = factory.memoir(
        account["id"], never_forget="He fed the whole street during the floods."
    )
    view = factory.link(str(memoir["id"]), scope="view")

    body = client.get(f"/r/{view['token']}").json()
    assert "never_forget" not in body
    assert "floods" not in response_text(body)


def response_text(payload) -> str:
    """Every string anywhere in a response, for leak assertions."""
    if isinstance(payload, dict):
        return " ".join(response_text(v) for v in payload.values())
    if isinstance(payload, list):
        return " ".join(response_text(v) for v in payload)
    return str(payload)


def test_the_index_of_people_lists_only_those_who_gave_something(client, book, factory):
    """Somebody who opened the link and sent nothing is not in the book.

    They are a real row, and they belong on the owner's contributors screen.
    The back matter is an index of the people *in* the memoir, and a name with
    nothing beside it reads as an accusation.
    """
    factory.contributor(book["memoir_id"], display_name="Never Replied")

    people = client.get(f"/r/{book['view_token']}").json()["people"]
    names = [p["name"] for p in people]

    assert "Margaret Reyes" in names
    assert "Never Replied" not in names


def test_a_contribute_link_does_not_open_the_book(client, book):
    """Scope is the whole point of there being two links.

    A link posted in a family group chat so people can send memories must not
    also hand out the finished memoir.
    """
    assert client.get(f"/r/{book['contribute_token']}").status_code == 404


def test_a_revoked_view_link_stops_working(client, book, factory):
    factory.revoke_link(book["view_link_id"])
    assert client.get(f"/r/{book['view_token']}").status_code == 404


def test_an_unknown_token_looks_exactly_like_a_revoked_one(client):
    assert client.get("/r/" + "0" * 48).status_code == 404


def test_the_owner_reads_their_own_memoir_without_a_link(as_owner, book):
    response = as_owner(book["owner_id"]).get(
        f"/memoirs/{book['memoir_id']}/chapters"
    )
    assert response.status_code == 200
    assert response.json()["totals"]["chapters"] == 1


def test_a_stranger_gets_404_for_somebody_elses_memoir(as_owner, book, stranger):
    """Never 403. This API does not confirm a stranger's memoir exists."""
    response = as_owner(str(stranger["account"]["id"])).get(
        f"/memoirs/{book['memoir_id']}/chapters"
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# One chapter
# ---------------------------------------------------------------------------


def test_a_chapter_comes_back_whole(client, storage, book):
    """Prose, photograph and credits in one response.

    A chapter is one page. Four round trips to draw it is four chances to show
    half of one.
    """
    response = client.get(
        f"/chapters/{book['chapter_id']}", headers=_link(book["view_token"])
    )
    assert response.status_code == 200

    chapter = response.json()
    assert chapter["title"] == "The House on Ellsworth Lane"

    kinds = [b["kind"] for b in chapter["blocks"]]
    assert kinds == ["paragraph", "figure", "paragraph"], "blocks come in reading order"

    assert chapter["memory_count"] == 2
    assert chapter["told_by"][0] == "Margaret Reyes", "most-cited first"


def test_a_paragraph_carries_the_people_it_was_assembled_from(client, storage, book):
    """The "never fabricate" rule, as a response body.

    Two accounts of one paragraph, one of them marked as differing, and the
    span of the words each supplied.
    """
    chapter = client.get(
        f"/chapters/{book['chapter_id']}", headers=_link(book["view_token"])
    ).json()
    first = chapter["blocks"][0]

    assert len(first["sources"]) == 2

    spanned = [s for s in first["sources"] if s["start_offset"] is not None][0]
    assert (spanned["start_offset"], spanned["end_offset"]) == (41, 81)
    assert spanned["name"] == "Margaret Reyes"
    assert spanned["medium"] == "voice"
    assert spanned["year"] is not None

    whole = [s for s in first["sources"] if s["start_offset"] is None][0]
    assert whole["diverges"] is True, "the account that contradicts is kept and marked"


def test_a_photograph_is_captioned_by_the_person_who_gave_it(client, storage, book):
    """Never by the machine.

    Inventing a description of somebody's photograph is exactly what the
    product forbids; the honest caption is already in the archive.
    """
    chapter = client.get(
        f"/chapters/{book['chapter_id']}", headers=_link(book["view_token"])
    ).json()
    figure = [b for b in chapter["blocks"] if b["kind"] == "figure"][0]["figure"]

    assert figure["caption"] == "She played worse."
    assert figure["credit"] == "Claire Donnelly"
    assert figure["placement"] == "margin"
    assert figure["anchor_block_id"] == book["first_block"]
    assert figure["url"].startswith("https://storage.test/read/")


def test_a_photographs_address_in_storage_never_leaves_the_api(client, storage, book):
    """`storage_path` is internal. Handing it out invites built URLs."""
    body = client.get(
        f"/chapters/{book['chapter_id']}", headers=_link(book["view_token"])
    ).json()
    assert "storage_path" not in response_text(body)


def test_a_chapter_needs_some_credential(client, book):
    """401, not 404. "Who are you" and "not yours" are different answers."""
    assert client.get(f"/chapters/{book['chapter_id']}").status_code == 401


def test_a_chapter_from_another_memoir_is_not_reachable(factory, client, book):
    """A live view link opens its own memoir and nothing else."""
    other_account = factory.account(name="Someone Else")
    other_memoir = factory.memoir(other_account["id"], subject_name="Another Subject")
    other_chapter = factory.chapter(str(other_memoir["id"]))

    response = client.get(
        f"/chapters/{other_chapter['id']}", headers=_link(book["view_token"])
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# The layer that stays open
# ---------------------------------------------------------------------------


def test_a_reader_leaves_a_comment_and_is_remembered(client, book):
    """The receipt carries the token that makes them the same person next time.

    The only response in this router that hands out a credential, and it goes
    to somebody who by design has no other one.
    """
    response = client.post(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["view_token"]),
        json={
            "block_id": book["first_block"],
            "body": "My mother told this one differently.",
            "display_name": "Claire Donnelly",
        },
    )
    assert response.status_code == 201

    receipt = response.json()
    assert TOKEN_PATTERN.match(receipt["participant_token"])
    assert receipt["thread"]["block_id"] == book["first_block"]
    assert receipt["thread"]["comments"][0]["name"] == "Claire Donnelly"
    assert receipt["thread"]["comments"][0]["is_owner"] is False


def test_a_comment_can_be_about_a_phrase_rather_than_a_paragraph(client, book):
    response = client.post(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["view_token"]),
        json={
            "block_id": book["first_block"],
            "start_offset": 41,
            "end_offset": 81,
            "body": "Damp, not the cold.",
            "display_name": "Thomas Marsh",
        },
    )
    assert response.status_code == 201

    thread = response.json()["thread"]
    assert (thread["start_offset"], thread["end_offset"]) == (41, 81)


def test_a_comment_cannot_point_past_the_end_of_the_passage(client, book):
    """The database can only tell that end > start.

    An offset past the end would store fine and then highlight nothing forever,
    in a memoir that cannot be corrected.
    """
    response = client.post(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["view_token"]),
        json={
            "block_id": book["first_block"],
            "start_offset": 5,
            "end_offset": 9000,
            "body": "Anchored to nothing.",
            "display_name": "Thomas Marsh",
        },
    )
    assert response.status_code == 400


def test_a_reader_must_say_who_they_are(client, book):
    """An unattributed comment in this product is worse than no comment."""
    response = client.post(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["view_token"]),
        json={"block_id": book["first_block"], "body": "Anonymous."},
    )
    assert response.status_code == 400


def test_a_returning_reader_is_the_same_person(client, book):
    """Two comments, one name in the memoir — not two Claire Donnellys."""
    first = client.post(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["view_token"]),
        json={
            "block_id": book["first_block"],
            "body": "One.",
            "display_name": "Claire Donnelly",
        },
    ).json()

    second = client.post(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["view_token"]),
        json={
            "block_id": book["second_block"],
            "body": "Two.",
            "display_name": "Claire Donnelly",
            "participant_token": first["participant_token"],
        },
    ).json()

    assert (
        second["thread"]["comments"][0]["participant_id"]
        == first["thread"]["comments"][0]["participant_id"]
    )


def test_a_reply_joins_the_conversation_it_answers(client, book):
    started = client.post(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["view_token"]),
        json={
            "block_id": book["first_block"],
            "body": "She played worse for a year.",
            "display_name": "Claire Donnelly",
        },
    ).json()

    replied = client.post(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["view_token"]),
        json={
            "thread_id": started["thread"]["id"],
            "body": "Both are true.",
            "display_name": "Margaret Reyes",
        },
    )
    assert replied.status_code == 201

    thread = replied.json()["thread"]
    assert thread["id"] == started["thread"]["id"]
    assert [c["body"] for c in thread["comments"]] == [
        "She played worse for a year.",
        "Both are true.",
    ], "oldest first"


def test_a_comment_names_exactly_one_target(client, book):
    """Both ids, or neither, is 422 at the edge rather than a server guess."""
    both = client.post(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["view_token"]),
        json={
            "block_id": book["first_block"],
            "thread_id": str(uuid.uuid4()),
            "body": "Which?",
            "display_name": "Thomas Marsh",
        },
    )
    assert both.status_code == 422

    neither = client.post(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["view_token"]),
        json={"body": "Which?", "display_name": "Thomas Marsh"},
    )
    assert neither.status_code == 422


def test_the_owner_comments_as_themselves_and_gets_no_token(as_owner, book):
    """They already have an account. A second, weaker credential is a liability."""
    response = as_owner(book["owner_id"]).post(
        f"/chapters/{book['chapter_id']}/comments",
        json={"block_id": book["first_block"], "body": "Ask Margaret about this."},
    )
    assert response.status_code == 201

    receipt = response.json()
    assert receipt["participant_token"] is None
    assert receipt["thread"]["comments"][0]["is_owner"] is True


def test_a_published_memoir_still_accepts_comments(client, factory, book):
    """The one write in this API that publication does not close.

    Every other write answers 409 once `status` flips. This is the layer the
    confirm screen makes people tick a box about: "the comment layer stays
    open… for as long as they want."
    """
    factory.publish(book["memoir_id"])

    response = client.post(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["view_token"]),
        json={
            "block_id": book["first_block"],
            "body": "Still here, years later.",
            "display_name": "Claire Donnelly",
        },
    )
    assert response.status_code == 201


def test_comments_can_be_read_back_on_their_own(client, book):
    """The reader polls this rather than re-fetching a chapter's whole prose."""
    client.post(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["view_token"]),
        json={
            "block_id": book["first_block"],
            "body": "One.",
            "display_name": "Claire Donnelly",
        },
    )

    response = client.get(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["view_token"]),
    )
    assert response.status_code == 200

    threads = response.json()
    assert len(threads) == 1
    assert threads[0]["comments"][0]["body"] == "One."


def test_comments_need_a_credential(client, book):
    assert client.get(f"/chapters/{book['chapter_id']}/comments").status_code == 401


def test_a_comment_cannot_be_posted_through_a_contribute_link(client, book):
    """Writing memories and reading the book are different permissions."""
    response = client.post(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["contribute_token"]),
        json={
            "block_id": book["first_block"],
            "body": "Wrong door.",
            "display_name": "Someone",
        },
    )
    assert response.status_code == 404


def test_a_comment_cannot_be_anchored_to_another_chapters_block(
    factory, client, book
):
    """A block id from elsewhere looks exactly like one that does not exist."""
    other = factory.chapter(book["memoir_id"], ordinal=1, title="A Second Chapter")
    elsewhere = factory.block(book["memoir_id"], str(other["id"]), text="Elsewhere.")

    response = client.post(
        f"/chapters/{book['chapter_id']}/comments",
        headers=_link(book["view_token"]),
        json={
            "block_id": str(elsewhere["id"]),
            "body": "Wrong chapter.",
            "display_name": "Thomas Marsh",
        },
    )
    assert response.status_code == 404
