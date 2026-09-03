"""The contract at the edge of the API.

Pydantic is the first of the two validation layers — good error messages at the
boundary, with a database constraint underneath doing the actual guaranteeing.
These tests cover the first layer, and record honestly where it has no opinion
and the database is carrying the rule alone.
"""

import pytest
from pydantic import ValidationError

from src.models.memory_models import (
    AssetAttachment,
    ContributedMemory,
    MemoryCreate,
    MemoryUpdate,
    UploadRequest,
)


# ---------------------------------------------------------------------------
# Partial updates
# ---------------------------------------------------------------------------


def test_an_absent_field_and_an_explicit_null_are_different_instructions():
    """The distinction the whole edit feature rests on.

    A PATCH sending only a title must leave the body alone. Without
    `exclude_unset`, Pydantic supplies None for everything unmentioned and
    editing a title silently wipes the text somebody wrote.

    Absent means "leave it"; an explicit null means "clear it". Both are things
    an edit form needs to be able to say.
    """
    only_title = MemoryUpdate.model_validate({"title": "New"})
    assert only_title.model_dump(exclude_unset=True) == {"title": "New"}

    clearing = MemoryUpdate.model_validate({"title": None})
    assert clearing.model_dump(exclude_unset=True) == {"title": None}

    assert MemoryUpdate().model_dump(exclude_unset=True) == {}


def test_kind_is_never_accepted_from_a_client():
    """A memory holds writing, photographs and recordings in any combination.

    What it *is* is derived from what it holds, so a client-sent `kind` could
    disagree with the contents and there would be no way to tell which was
    right. Neither create model has the field.
    """
    assert "kind" not in MemoryCreate.model_fields
    assert "kind" not in ContributedMemory.model_fields
    assert "kind" not in MemoryUpdate.model_fields

    assert MemoryCreate.model_validate(
        {"body_text": "x", "kind": "voice"}
    ).model_dump(exclude_unset=True) == {"body_text": "x"}


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_a_title_is_bounded_on_create():
    MemoryCreate(title="x" * 200)

    with pytest.raises(ValidationError):
        MemoryCreate(title="x" * 201)


def test_a_title_is_not_bounded_on_update():
    """Recorded, not endorsed.

    `MemoryCreate.title` is capped at 200 characters and `MemoryUpdate.title` is
    not, so a title too long to create can be reached by creating a short one
    and editing it. Nothing downstream breaks — the column is `text` — but the
    two halves of the same field disagree, which is the kind of asymmetry that
    turns into a display bug later.
    """
    assert MemoryUpdate(title="x" * 5000).title is not None


def test_a_contributor_must_give_a_name():
    """There is no account to look one up from.

    The family would otherwise receive a memory from nobody, which is the one
    piece of context that cannot be reconstructed afterwards.
    """
    with pytest.raises(ValidationError):
        ContributedMemory(display_name="")

    with pytest.raises(ValidationError):
        ContributedMemory(display_name="x" * 121)


def test_whitespace_passes_pydantic_and_is_caught_underneath():
    """`min_length=1` counts characters, so "   " gets through here.

    Recorded because it is exactly the case where the second validation layer
    earns its keep: `_resolve_contributor` strips it, and the database's
    `participant_name_not_blank` refuses the result. A rule enforced in only one
    place would have let a blank name through.
    """
    assert ContributedMemory(display_name="   ").display_name == "   "


def test_attaching_assets_requires_at_least_one():
    """An empty attach is a request that means nothing.

    422 rather than a 200 that did nothing, so a client bug surfaces as a bug.
    """
    AssetAttachment(asset_ids=["00000000-0000-0000-0000-000000000001"])

    with pytest.raises(ValidationError):
        AssetAttachment(asset_ids=[])


# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------


def test_asset_kind_is_closed():
    """A `Literal`, so a wrong value is a clean 422 at the edge.

    Left to the database it would arrive as SQLSTATE 22P02 and become a much
    less legible 400 about an invalid value, with no field name attached.
    """
    UploadRequest(
        memoir_id="00000000-0000-0000-0000-000000000001",
        kind="image",
        mime_type="image/jpeg",
    )

    for wrong in ["video", "Image", "audio/webm", ""]:
        with pytest.raises(ValidationError):
            UploadRequest(
                memoir_id="00000000-0000-0000-0000-000000000001",
                kind=wrong,
                mime_type="image/jpeg",
            )


def test_a_duration_must_be_positive_if_given():
    """Mirrors `asset_duration_positive` in the schema.

    The only numeric bound anywhere in these models, and it exists because a
    zero or negative duration would be stored and then summed into somebody's
    transcription budget.
    """
    UploadRequest(
        memoir_id="00000000-0000-0000-0000-000000000001",
        kind="audio",
        mime_type="audio/webm",
        duration_ms=1,
    )

    for wrong in [0, -1]:
        with pytest.raises(ValidationError):
            UploadRequest(
                memoir_id="00000000-0000-0000-0000-000000000001",
                kind="audio",
                mime_type="audio/webm",
                duration_ms=wrong,
            )


def test_body_text_is_unbounded():
    """No maximum, anywhere, and no request-size limit in the app either.

    Deliberate at this stage — somebody's account of an afternoon should not hit
    a character count — but it means a single request can be arbitrarily large.
    Written down here so it is a known position rather than an oversight.
    """
    assert MemoryCreate(body_text="x" * 1_000_000).body_text is not None


def test_a_malformed_uuid_is_refused_at_the_edge():
    with pytest.raises(ValidationError):
        UploadRequest(memoir_id="not-a-uuid", kind="image", mime_type="image/jpeg")
