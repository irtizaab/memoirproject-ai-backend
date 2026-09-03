"""Transcript shaping, and the money it protects.

The pure parts of `domain/transcripts/` — what gets kept from a provider's
answer, and what is dropped before it reaches anyone. No database.
"""

import pytest

from src.domain.transcripts.transcript_service import (
    TERMINAL,
    _audio_seconds,
    to_payload,
)


# ---------------------------------------------------------------------------
# What leaves the domain layer
# ---------------------------------------------------------------------------


def test_a_payload_carries_five_fields_and_no_more():
    """The first of the two filters on transcript internals.

    `provider_id` is the handle the webhook is addressed by, and `error` is a
    provider's message that a grieving family can do nothing with. Both are
    selected by the query — reconcile needs one, the log wants the other — and
    both stop here.
    """
    row = {
        "status": "done",
        "text": "What was said.",
        "segments": [{"start": 0, "end": 1000, "text": "What was said."}],
        "language_code": "en",
        "confidence": 0.94,
        "provider_id": "assemblyai-job-1",
        "error": None,
        "requested_at": "2026-01-01",
        "asset_id": "irrelevant",
    }

    payload = to_payload(row)

    assert set(payload) == {
        "status",
        "text",
        "segments",
        "language_code",
        "confidence",
    }
    assert "provider_id" not in payload
    assert "error" not in payload


def test_no_transcript_is_none_rather_than_an_empty_shape():
    """A photograph has no transcript, and should not carry a hollow one."""
    assert to_payload(None) is None


def test_segments_arriving_as_a_string_are_parsed_rather_than_crashing():
    """Defensive, cheaply.

    psycopg returns jsonb already parsed, so this should never happen — but a
    row written by something else could hold a string, and the difference
    between parsing it and not is a missing paragraph break versus a 500 on the
    archive page.
    """
    row = {
        "status": "done",
        "text": "x",
        "segments": '[{"start": 0, "end": 1, "text": "x"}]',
        "language_code": None,
        "confidence": None,
    }

    assert to_payload(row)["segments"] == [{"start": 0, "end": 1, "text": "x"}]


def test_unparseable_segments_become_none_not_an_exception():
    row = {
        "status": "done",
        "text": "x",
        "segments": "{not json",
        "language_code": None,
        "confidence": None,
    }

    assert to_payload(row)["segments"] is None


# ---------------------------------------------------------------------------
# The number the budget is summed from
# ---------------------------------------------------------------------------


class TestAudioSeconds:
    """How long the provider says the recording was.

    This is the figure the transcription budget prefers, over the duration the
    *client* sent. A budget summed from a number the limited party chooses is
    not a budget — a modified browser could report every recording as one second
    and transcribe forever.

    So it has to be read defensively: a provider that renames the field or sends
    something unparseable must not take down a transcript that is otherwise
    perfectly good. None simply means the budget keeps using the estimate for
    this one.
    """

    def test_a_normal_duration(self):
        assert _audio_seconds({"audio_duration": 125}) == 125

    def test_a_float_is_truncated_to_whole_seconds(self):
        assert _audio_seconds({"audio_duration": 125.9}) == 125

    def test_a_numeric_string_is_accepted(self):
        assert _audio_seconds({"audio_duration": "125"}) == 125

    @pytest.mark.parametrize(
        "value", [None, "", "not-a-number", {}, [], float("nan")]
    )
    def test_anything_unusable_is_none(self, value):
        assert _audio_seconds({"audio_duration": value}) is None

    def test_a_missing_field_is_none(self):
        assert _audio_seconds({}) is None

    @pytest.mark.parametrize("value", [0, -5])
    def test_a_non_positive_duration_is_none(self, value):
        """Mirrors `transcript_audio_seconds_positive` in the schema.

        Zero would also be a lie about a recording that exists.
        """
        assert _audio_seconds({"audio_duration": value}) is None


# ---------------------------------------------------------------------------
# Which states are finished
# ---------------------------------------------------------------------------


def test_the_terminal_states_are_the_three_that_never_change():
    """What the poller stops chasing, and what the frontend stops refetching.

    `skipped` is in here and that is the interesting one — it means a deliberate
    decision not to spend, so no retry pass should ever pick it up later and
    spend it anyway. A recording skipped for budget stays skipped.
    """
    assert set(TERMINAL) == {"done", "failed", "skipped"}
