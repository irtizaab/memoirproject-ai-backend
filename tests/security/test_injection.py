"""Untrusted input reaching places it should not.

Two families here. SQL injection, which this codebase defends against by never
interpolating a value into query text — tested at the two places that build a
SET clause dynamically, since those are the only ones where the shape of the
query depends on what arrived. And stored XSS through an upload, which is what
the mime allow-list exists for.
"""

import pytest

from tests.conftest import requires_db

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# The mime allow-list. Pure function — no database needed.
# ---------------------------------------------------------------------------


class TestTheUploadAllowList:
    """What a browser is permitted to be given back later.

    Every uploaded file is served to browsers, and the browser decides what to
    do with it from the type travelling alongside. Two types tell it *to run
    code*, from your own domain, with your own site's privileges. That is stored
    XSS, and it is one of the oldest attacks on the web.

    So the accepted types are a written list of thirteen rather than whatever
    the uploader claims. Anything nobody thought about is refused by default,
    which is the property that matters — a block-list has to anticipate every
    dangerous type forever, and will eventually miss one.
    """

    @pytest.mark.parametrize(
        "mime",
        [
            "image/svg+xml",  # SVG carries <script>
            "text/html",
            "application/xhtml+xml",
            "application/javascript",
            "text/javascript",
            "application/pdf",
            "application/octet-stream",
            "image/svg",
            "video/mp4",
            "text/plain",
        ],
    )
    def test_dangerous_and_unknown_types_are_refused(self, mime):
        from src.domain.media.media_service import UnsupportedMediaType, _extension_for

        with pytest.raises(UnsupportedMediaType):
            _extension_for("image", mime)

    @pytest.mark.parametrize(
        "mime,extension",
        [
            ("image/jpeg", "jpg"),
            ("image/png", "png"),
            ("image/webp", "webp"),
            ("image/heic", "heic"),
            ("image/gif", "gif"),
        ],
    )
    def test_real_image_types_are_accepted(self, mime, extension):
        from src.domain.media.media_service import _extension_for

        assert _extension_for("image", mime) == extension

    @pytest.mark.parametrize(
        "mime,extension",
        [
            ("audio/webm", "webm"),
            ("audio/ogg", "ogg"),
            ("audio/mp4", "m4a"),
            ("audio/x-m4a", "m4a"),
            ("audio/mpeg", "mp3"),
            ("audio/wav", "wav"),
            ("audio/aac", "aac"),
        ],
    )
    def test_real_audio_types_are_accepted(self, mime, extension):
        from src.domain.media.media_service import _extension_for

        assert _extension_for("audio", mime) == extension

    def test_codec_parameters_are_stripped_not_rejected(self):
        """`audio/webm;codecs=opus` is what MediaRecorder actually reports.

        Matching on the full string would refuse every recording Chrome makes —
        a correctness bug wearing security clothing. The base type is what is
        checked, after lowering and trimming.
        """
        from src.domain.media.media_service import _extension_for

        assert _extension_for("audio", "audio/webm;codecs=opus") == "webm"
        assert _extension_for("audio", "  AUDIO/WEBM ; codecs=opus ") == "webm"
        assert _extension_for("image", "IMAGE/JPEG") == "jpg"

    def test_the_two_tables_do_not_bleed_into_each_other(self):
        """A kind of `audio` cannot smuggle an image type through, or the reverse."""
        from src.domain.media.media_service import UnsupportedMediaType, _extension_for

        with pytest.raises(UnsupportedMediaType):
            _extension_for("audio", "image/png")

        with pytest.raises(UnsupportedMediaType):
            _extension_for("image", "audio/webm")


# ---------------------------------------------------------------------------
# The allow-list, through the real route
# ---------------------------------------------------------------------------


@requires_db
@pytest.mark.db
def test_a_refused_type_is_a_400_naming_it(as_owner, owner):
    """The client's mistake, said plainly, without leaking anything else.

    Note the ordering it depends on: `_extension_for` runs *before* the
    authorization check, so a bad mime type is a 400 whether or not the caller
    owns the memoir. That leaks nothing — the answer is identical for a memoir
    that exists and one that does not.
    """
    response = as_owner(owner["account"]["id"]).post(
        "/media/uploads",
        json={
            "memoir_id": str(owner["memoir"]["id"]),
            "kind": "image",
            "mime_type": "image/svg+xml",
        },
    )

    assert response.status_code == 400
    assert "image/svg+xml" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Object keys
# ---------------------------------------------------------------------------


@requires_db
@pytest.mark.db
def test_the_original_filename_never_reaches_the_storage_path(
    as_owner, owner, storage
):
    """Keys are random, not derived from what was uploaded.

    Two reasons, and both are real. A name like `dads-funeral-2019.jpg` in a URL
    leaks something private to anyone who sees the address. And two people
    uploading `IMG_0001.jpg` would collide on the second one — `storage_path` is
    UNIQUE, so that is a 409 in the middle of somebody's contribution.

    The filename is still stored, for display. It is simply never a path.
    """
    response = as_owner(owner["account"]["id"]).post(
        "/media/uploads",
        json={
            "memoir_id": str(owner["memoir"]["id"]),
            "kind": "image",
            "mime_type": "image/jpeg",
            "original_filename": "dads-funeral-2019.jpg",
        },
    )

    assert response.status_code == 201
    assert storage.signed_uploads, "nothing was signed"

    path = storage.signed_uploads[-1]
    assert "dads-funeral" not in path
    assert path.startswith(f"{owner['memoir']['id']}/")
    assert path.endswith(".jpg")


@requires_db
@pytest.mark.db
def test_a_traversal_attempt_in_the_filename_changes_nothing(
    as_owner, owner, storage
):
    """`../../` in a filename cannot escape the memoir's prefix.

    Not because it is sanitised — because the filename is never used. The key is
    the memoir id and a random hex string, and that is the entire input.
    """
    as_owner(owner["account"]["id"]).post(
        "/media/uploads",
        json={
            "memoir_id": str(owner["memoir"]["id"]),
            "kind": "image",
            "mime_type": "image/jpeg",
            "original_filename": "../../../etc/passwd",
        },
    )

    path = storage.signed_uploads[-1]
    assert ".." not in path
    assert path.startswith(f"{owner['memoir']['id']}/")


# ---------------------------------------------------------------------------
# SQL injection through the two dynamic SET clauses
# ---------------------------------------------------------------------------


def test_draft_update_drops_unknown_fields():
    """The weaker of the two dynamic queries, and why it is still safe.

    `update_draft` builds `SET {name} = %({name})s` from whichever keys arrived.
    Nothing filters those names against a list — the only thing between a
    crafted key and the query text is Pydantic having dropped it first.

    That works because `DraftUpdate` declares its fields and pydantic ignores
    the rest. It would stop working the moment somebody added
    `model_config = ConfigDict(extra="allow")`, which is a one-line change that
    reads as harmless. Hence this test.
    """
    from src.models.draft_models import DraftUpdate

    hostile = DraftUpdate.model_validate(
        {
            "subject_name": "Nusrat",
            "subject_name = 'x', claimed_at = now() --": "injected",
            "'; DROP TABLE memoir_draft; --": 1,
        }
    )

    assert set(hostile.model_dump(exclude_unset=True)) == {"subject_name"}
    assert DraftUpdate.model_config.get("extra") != "allow", (
        "DraftUpdate now accepts extra fields, and update_draft interpolates "
        "field names straight into its SET clause — that is SQL injection"
    )


def test_memory_update_filters_column_names_through_a_literal_set():
    """The stronger of the two.

    `update_memory` keeps a hardcoded `{"title", "body_text", "happened_on"}` and
    drops everything else before building the clause, so it is safe even if the
    model changed underneath it. When no key survives, `columns` is empty and the
    function returns None — which the route turns into a 404.
    """
    from src.models.memory_models import MemoryUpdate

    hostile = MemoryUpdate.model_validate(
        {"title = 'x' WHERE 1=1 --": "injected", "kind": "voice"}
    )
    assert hostile.model_dump(exclude_unset=True) == {}


@requires_db
@pytest.mark.db
def test_a_crafted_key_cannot_reach_the_database(as_owner, owner, factory, storage):
    """End to end, through the real route.

    The two tests above assert the mechanism; this one asserts the outcome. A
    request whose only keys are hostile changes nothing and leaves the memory
    exactly as it was.
    """
    memory = factory.memory(
        owner["memoir"]["id"],
        owner["memoir"]["owner_participant_id"],
        title="Untouched",
    )

    response = as_owner(owner["account"]["id"]).patch(
        f"/memories/{memory['id']}",
        json={"body_text'; DROP TABLE memory; --": "x", "kind": "voice"},
    )

    # No recognised field arrived, so there is nothing to update.
    assert response.status_code in (400, 404)

    still = as_owner(owner["account"]["id"]).get(f"/memories/{memory['id']}")
    assert still.status_code == 200
    assert still.json()["title"] == "Untouched"


@requires_db
@pytest.mark.db
def test_hostile_strings_are_stored_as_data_not_executed(
    as_owner, owner, factory, storage
):
    """A memory whose text is a SQL fragment is just a memory.

    Values travel as `%(name)s` and are bound by the driver, so the database
    never sees them as instructions. Somebody writing about a table called
    `memory` should not break anything.
    """
    payloads = [
        "'; DROP TABLE memory; --",
        "1' OR '1'='1",
        "\\'; DELETE FROM memoir WHERE 'a'='a",
        "<script>alert(1)</script>",
    ]

    client = as_owner(owner["account"]["id"])

    for text in payloads:
        created = client.post(
            f"/memoirs/{owner['memoir']['id']}/memories", json={"body_text": text}
        )
        assert created.status_code == 201, text
        assert created.json()["body_text"] == text, "the value was altered in transit"

    listing = client.get(f"/memoirs/{owner['memoir']['id']}/memories")
    assert len(listing.json()) == len(payloads), "a table went missing"
