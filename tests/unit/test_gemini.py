# The one piece of `integrations/gemini.py` that is logic rather than plumbing:
# turning a Pydantic class into a schema Gemini will accept.
#
# It earns a test because its failure mode is remote and expensive. A `$ref`
# left in the schema is a 400 from Google with a message about an unsupported
# field, arriving on a contributor's phone, at which point the interesting
# question — which nested model produced it — is nowhere in the error.
#
# No network, no key, no database. These are all pure functions.

import asyncio

import pytest
from pydantic import BaseModel, Field

from src.integrations.gemini import _schema_for


class Leaf(BaseModel):
    quote: str | None = None
    diverges: bool = False


class Branch(BaseModel):
    kind: str = Field(default="paragraph", description="what this is")
    leaves: list[Leaf] = Field(default_factory=list)


class Trunk(BaseModel):
    branches: list[Branch]
    note: Leaf | None = None


def _walk(node):
    """Every dict in the schema, so a test can assert about all of them."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def test_no_refs_survive():
    """Nested models are inlined, not referenced. The whole point of the file."""
    schema = _schema_for(Trunk)

    assert "$defs" not in schema
    assert all("$ref" not in node for node in _walk(schema))


def test_unsupported_keys_are_stripped():
    """`title`, `default` and `additionalProperties` are dropped everywhere.

    Not cosmetic: Gemini rejects the schema outright on some of them.
    """
    schema = _schema_for(Trunk)

    for node in _walk(schema):
        assert "additionalProperties" not in node
        assert "title" not in node
        assert "default" not in node


def test_structure_survives_the_flattening():
    """Stripping is not allowed to cost the parts that constrain the output."""
    schema = _schema_for(Trunk)

    assert schema["type"] == "object"
    leaves = schema["properties"]["branches"]["items"]["properties"]["leaves"]
    assert leaves["type"] == "array"

    # Two levels of nesting, inlined, with its own fields intact.
    quote = leaves["items"]["properties"]["quote"]
    assert quote  # not an empty dict left behind by the $ref resolution

    # `required` is what stops a model omitting a field we then read.
    assert "branches" in schema["required"]


def test_descriptions_survive():
    """The description is the instruction. Losing it loses the rule."""
    schema = _schema_for(Branch)
    assert schema["properties"]["kind"]["description"] == "what this is"


def test_generate_refuses_without_a_key(monkeypatch):
    """No key is `GeminiDisabled`, which every caller treats as "ask nothing".

    Checked here because the alternative — a request going out with an empty
    `x-goog-api-key` header — fails as a 400 that looks like a bad prompt.
    """
    from src.core.config import settings
    from src.integrations import gemini

    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "ai_enabled", True)

    with pytest.raises(gemini.GeminiDisabled):
        asyncio.run(gemini.generate("anything", Leaf))


def test_generate_refuses_when_switched_off(monkeypatch):
    """The kill switch beats a present key, or it is not a kill switch."""
    from src.core.config import settings
    from src.integrations import gemini

    monkeypatch.setattr(settings, "gemini_api_key", "a-real-looking-key")
    monkeypatch.setattr(settings, "ai_enabled", False)

    with pytest.raises(gemini.GeminiDisabled):
        asyncio.run(gemini.generate("anything", Leaf))


# ---------------------------------------------------------------------------
# Retrying a momentarily overloaded model
# ---------------------------------------------------------------------------
#
# Gemini answers 503 UNAVAILABLE when the model is busy, with no warning and no
# pattern: six identical calls will fail one and serve the other five. Without a
# retry that reaches the owner as "these could not be written just now" on a
# button that worked a minute ago and will work again a minute later.


class _Response:
    """Just enough of httpx.Response for `_post` to decide about it."""

    def __init__(self, status_code: int):
        self.status_code = status_code


def _no_waiting(monkeypatch):
    """Run the backoff instantly, so the test does not sleep for four seconds."""
    from src.integrations import gemini

    async def _instant(_seconds):
        return None

    monkeypatch.setattr(gemini.asyncio, "sleep", _instant)


def test_a_transient_503_is_retried(monkeypatch):
    """The failure this was written for."""
    from src.integrations import gemini

    _no_waiting(monkeypatch)
    answers = [_Response(503), _Response(200)]
    attempts = []

    async def _fake_client():
        class _Client:
            async def post(self, *args, **kwargs):
                attempts.append(1)
                return answers.pop(0)

        return _Client()

    monkeypatch.setattr(gemini, "client", _fake_client)

    result = asyncio.run(gemini._post("a-model", {}, 60.0))

    assert result.status_code == 200
    assert len(attempts) == 2


def test_a_400_is_not_retried(monkeypatch):
    """A malformed request fails identically forever.

    Retrying it only makes somebody wait three times as long for the same
    error, so `_RETRY_ON` deliberately does not include it.
    """
    from src.integrations import gemini

    _no_waiting(monkeypatch)
    attempts = []

    async def _fake_client():
        class _Client:
            async def post(self, *args, **kwargs):
                attempts.append(1)
                return _Response(400)

        return _Client()

    monkeypatch.setattr(gemini, "client", _fake_client)

    assert asyncio.run(gemini._post("a-model", {}, 60.0)).status_code == 400
    assert len(attempts) == 1


def test_retries_are_bounded(monkeypatch):
    """A model that is down stays down, and the caller is told so.

    Three attempts total, then the last response is handed back for `generate`
    to turn into a `GeminiError`. An unbounded retry on a request somebody is
    watching is a spinner that never stops.
    """
    from src.integrations import gemini

    _no_waiting(monkeypatch)
    attempts = []

    async def _fake_client():
        class _Client:
            async def post(self, *args, **kwargs):
                attempts.append(1)
                return _Response(503)

        return _Client()

    monkeypatch.setattr(gemini, "client", _fake_client)

    assert asyncio.run(gemini._post("a-model", {}, 60.0)).status_code == 503
    assert len(attempts) == 3
