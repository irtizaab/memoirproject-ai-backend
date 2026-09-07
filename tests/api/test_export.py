"""The memoir as a file somebody can keep.

A PDF is hard to assert about: it is a binary format and the useful questions
("does it look like a book") are not ones a test can answer. So these check the
things that would be wrong in a way nobody notices until a family has printed
it — a name missing, somebody's credit missing, the comment layer leaking into
a document that is supposed to be finished — plus the ordinary access rules.
"""

import base64
import re
import uuid
import zlib

import pytest

from tests.conftest import requires_db
from tests.factories import TEST_PASSPHRASE

pytestmark = [requires_db, pytest.mark.db]


def pdf_text(body: bytes) -> str:
    """Every readable string in a PDF, flattened.

    ReportLab writes page streams through ASCII85 and then Flate, so the words
    are neither plain bytes nor plain zlib. This undoes both where it can and
    pulls out the parenthesised strings — enough to answer "is this name in the
    document", which is all any test here asks.
    """
    found: list[str] = []

    for raw in re.findall(rb"stream\r?\n(.*?)endstream", body, re.S):
        # Stripped, because a85decode will not tolerate the newline ReportLab
        # writes between the `~>` terminator and `endstream`.
        content = raw.strip()
        try:
            content = base64.a85decode(content, adobe=True)
        except ValueError:
            pass
        try:
            content = zlib.decompress(content)
        except zlib.error:
            pass

        for chunk in re.findall(rb"\((.*?)\)", content, re.S):
            found.append(chunk.decode("latin-1", "replace"))

    # A backslash-escaped bracket inside a PDF string is a literal one.
    return " ".join(found).replace("\\(", "(").replace("\\)", ")")


@pytest.fixture
def book(factory, owner):
    """A memoir with two chapters, two contributors and a disagreement."""
    memoir_id = str(owner["memoir"]["id"])

    margaret = factory.contributor(memoir_id, display_name="Margaret Reyes")
    claire = factory.contributor(memoir_id, display_name="Claire Donnelly")

    hers = factory.memory(
        memoir_id, margaret["id"], kind="voice", title=None,
        body_text="She was talking to it.", happened_on="1994-05-01",
    )
    theirs = factory.memory(
        memoir_id, claire["id"], kind="text", title=None,
        body_text="She played worse.", happened_on="1995-02-01",
    )

    first = factory.chapter(memoir_id, ordinal=0, title="The House on Ellsworth Lane")
    second = factory.chapter(memoir_id, ordinal=1, title="A Room with a North Window")

    opening = factory.block(
        memoir_id, str(first["id"]), ordinal=0,
        text="The Chickering was not a good instrument.",
    )
    factory.source(
        memoir_id, str(opening["id"]),
        memory_id=str(hers["id"]), participant_id=str(margaret["id"]),
    )
    factory.source(
        memoir_id, str(opening["id"]),
        memory_id=str(theirs["id"]), participant_id=str(claire["id"]),
        diverges=True,
    )

    later = factory.block(
        memoir_id, str(second["id"]), ordinal=0,
        text="The room at the back that nobody wanted.",
    )
    factory.source(
        memoir_id, str(later["id"]),
        memory_id=str(hers["id"]), participant_id=str(margaret["id"]),
    )

    return {
        "memoir_id": memoir_id,
        "owner_id": str(owner["account"]["id"]),
        "chapter_id": str(first["id"]),
    }


def _export(as_owner, book):
    return as_owner(book["owner_id"]).get(f"/memoirs/{book['memoir_id']}/export.pdf")


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def test_the_export_is_a_pdf_the_browser_will_save(as_owner, book):
    response = _export(as_owner, book)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    assert "attachment" in response.headers["content-disposition"]
    assert "Nusrat Bibi.pdf" in response.headers["content-disposition"]


def test_the_book_carries_the_subjects_name_and_every_chapter(as_owner, book):
    text = pdf_text(_export(as_owner, book).content)

    assert "Nusrat Bibi" in text
    assert "The House on Ellsworth Lane" in text
    assert "A Room with a North Window" in text
    assert "Contents" in text


def test_the_prose_is_there(as_owner, book):
    text = pdf_text(_export(as_owner, book).content)
    assert "Chickering" in text
    assert "nobody wanted" in text


def test_every_paragraph_still_says_who_it_came_from(as_owner, book):
    """The whole product constraint, carried into print.

    On screen a credit lights up the clause it fathered. On paper it is a
    numbered note under the chapter — the same fact, in the only form a printed
    page has for it.
    """
    text = pdf_text(_export(as_owner, book).content)

    assert "Margaret Reyes" in text
    assert "Claire Donnelly" in text
    assert "recorded" in text


def test_an_account_that_disagrees_is_marked_as_such(as_owner, book):
    """Both are kept and neither is corrected, so the note has to say which is
    which — otherwise the page is quietly choosing one."""
    assert "differs" in pdf_text(_export(as_owner, book).content)


def test_the_conversation_is_not_in_the_file(as_owner, client, factory, book):
    """The comment layer is the one part of a memoir that is still growing.

    Printing it freezes half a conversation into something that looks final,
    and the family would have no way to tell which half.
    """
    factory.link(book["memoir_id"], scope="view")
    factory.publish(book["memoir_id"])

    as_owner(book["owner_id"]).post(
        f"/chapters/{book['chapter_id']}/comments",
        json={
            "block_id": None,
            "thread_id": None,
            "body": "PLEASE-DO-NOT-PRINT-THIS",
        },
    )

    text = pdf_text(_export(as_owner, book).content)
    assert "PLEASE-DO-NOT-PRINT-THIS" not in text


def test_a_memoir_with_awkward_characters_still_builds(as_owner, factory, owner):
    """An ampersand or an angle bracket in somebody's memory.

    ReportLab reads a subset of HTML inside a paragraph, so unescaped text is
    not a formatting problem — it is a raised exception, or a sentence that
    silently loses half of itself.
    """
    memoir_id = str(owner["memoir"]["id"])
    person = factory.contributor(memoir_id, display_name="Ana <Whitfield> & Co")
    memory = factory.memory(
        memoir_id, person["id"], kind="text", title=None,
        body_text="Marks & Spencer, 5 < 6, and a <thing> she said.",
    )
    chapter = factory.chapter(memoir_id, ordinal=0)
    block = factory.block(
        memoir_id, str(chapter["id"]), ordinal=0,
        text="Marks & Spencer, 5 < 6, and a <thing> she said.",
    )
    factory.source(
        memoir_id, str(block["id"]),
        memory_id=str(memory["id"]), participant_id=str(person["id"]),
    )

    response = as_owner(str(owner["account"]["id"])).get(
        f"/memoirs/{memoir_id}/export.pdf"
    )

    assert response.status_code == 200
    assert "Marks & Spencer" in pdf_text(response.content)


# ---------------------------------------------------------------------------
# Who may have it
# ---------------------------------------------------------------------------


def test_an_unassembled_memoir_has_nothing_to_export(as_owner, owner):
    response = as_owner(str(owner["account"]["id"])).get(
        f"/memoirs/{owner['memoir']['id']}/export.pdf"
    )

    assert response.status_code == 400
    assert "assemble" in response.json()["detail"]


def test_a_stranger_cannot_export_someone_elses_memoir(as_owner, stranger, book):
    """404, never 403 — and the worst possible leak in the product if wrong:
    the whole memoir, in one file, in one request."""
    response = as_owner(str(stranger["account"]["id"])).get(
        f"/memoirs/{book['memoir_id']}/export.pdf"
    )

    assert response.status_code == 404


def test_a_reader_with_a_view_link_cannot_export(client, factory, book):
    """A link lets somebody read the book. Handing every reader a print-ready
    copy of a private family memoir is a different decision, and nobody has
    made it."""
    view = factory.link(book["memoir_id"], scope="view")
    factory.publish(book["memoir_id"])

    response = client.get(
        f"/memoirs/{book['memoir_id']}/export.pdf",
        headers={"X-Link-Token": view["token"]},
    )

    assert response.status_code == 401


def test_exporting_a_memoir_that_does_not_exist_answers_the_same_way(as_owner, book):
    response = as_owner(book["owner_id"]).get(f"/memoirs/{uuid.uuid4()}/export.pdf")
    assert response.status_code == 404
