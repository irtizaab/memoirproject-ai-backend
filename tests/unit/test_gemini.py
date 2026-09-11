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


def test_a_field_called_title_survives():
    """Stripping the `title` keyword may not strip a field named `title`.

    The regression this exists for: `properties` is a mapping of field names,
    and the strip ran over it like any other dict, so `PlannedChapter.title`
    was deleted from `properties` while `required` still demanded it. Gemini
    answers `required[0]: property is not defined` — a 400, which
    `planner.plan()` turns into None, which is the decade fallback again. The
    nullable fix was necessary and this was the other half.
    """

    class Titled(BaseModel):
        title: str = Field(description="what the chapter is called")
        default: int = 0

    schema = _schema_for(Titled)

    assert schema["properties"]["title"]["type"] == "string"
    assert schema["properties"]["title"]["description"] == "what the chapter is called"
    assert "default" in schema["properties"]

    # Every name `required` promises is actually defined — the exact thing
    # Google checks before it looks at anything else.
    assert set(schema.get("required", [])) <= set(schema["properties"])


def test_every_required_field_of_a_plan_is_defined():
    """The same check against the real model, where it actually broke."""
    from src.domain.chapters.planner import Plan

    for node in _walk(_schema_for(Plan)):
        if "required" in node:
            assert set(node["required"]) <= set(node.get("properties", {}))


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


def test_an_optional_field_becomes_nullable_not_a_null_type():
    """`str | None` has to reach Gemini as `nullable`, never as `type: null`.

    The regression this exists for: Gemini's schema is OpenAPI 3.0, which has
    no null type, and it answers 400 to the whole request rather than ignoring
    the branch it dislikes. `planner.plan()` turns any 400 into None and falls
    back to the decade assembler — so one optional field in a response model
    was indistinguishable, from the outside, from the AI being switched off.
    Which is what it had been doing to every memoir.
    """
    schema = _schema_for(Leaf)
    quote = schema["properties"]["quote"]

    assert quote["type"] == "string"
    assert quote["nullable"] is True
    assert "anyOf" not in quote

    # And nowhere in the whole document, at any depth.
    for node in _walk(_schema_for(Trunk)):
        assert node.get("type") != "null"
        assert "anyOf" not in node


def test_a_nullable_field_keeps_its_description():
    """The description is the instruction, and collapsing must not eat it.

    `PlannedSource.quote` carries the rule that makes attribution checkable —
    "copied character for character out of the text you just wrote". It is
    written on the optional field, which is precisely the one being rewritten.
    """

    class Described(BaseModel):
        quote: str | None = Field(default=None, description="quote your own paragraph")

    quote = _schema_for(Described)["properties"]["quote"]

    assert quote["description"] == "quote your own paragraph"
    assert quote["nullable"] is True


def test_a_union_of_two_real_types_is_left_alone():
    """Only the optional shape is collapsed, not any union.

    Choosing a winner between two real branches would need a judgement nothing
    here is entitled to make. Nothing in this codebase sends such a union; if
    something starts to, it should fail loudly at Gemini rather than quietly
    become one arbitrary half of what was asked for.
    """

    class Either(BaseModel):
        value: int | str

    value = _schema_for(Either)["properties"]["value"]

    assert "anyOf" in value
    assert "nullable" not in value


def test_a_503_is_retried_on_the_schedule_it_was_given(monkeypatch):
    """Assembly waits, because assembly can afford to.

    The regression: `gemini-3.5-flash` answered "this model is currently
    experiencing high demand" three times inside seventeen seconds, the default
    two-step schedule ran out, and a family's memoir was organised by decade
    with five minutes of its own timeout unspent. The retry budget has to be
    the caller's decision — a person waiting on a follow-up question and an
    owner waiting on a whole book are not the same wait.
    """
    from src.core.config import settings
    from src.integrations import gemini

    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_enabled", True)

    slept: list[float] = []
    attempts = 0

    class Answer:
        """A 503 for every attempt but the last."""

        def __init__(self, status: int, body: str = "{}"):
            self.status_code = status
            self.text = body

        def json(self):
            return {
                "candidates": [
                    {"content": {"parts": [{"text": '{"quote": null}'}]}}
                ]
            }

    class Client:
        async def post(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            return Answer(503) if attempts < 4 else Answer(200)

    async def fake_client():
        return Client()

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(gemini, "client", fake_client)
    monkeypatch.setattr(gemini.asyncio, "sleep", fake_sleep)

    asyncio.run(gemini.generate("anything", Leaf, backoff=(5.0, 15.0, 40.0)))

    assert attempts == 4
    assert slept == [5.0, 15.0, 40.0]


def test_the_default_schedule_is_the_short_one(monkeypatch):
    """A contributor waiting on one question is not made to wait a minute."""
    from src.integrations import gemini

    assert gemini._BACKOFF_SECONDS == (1.0, 3.0)
    # And the patient one is a real budget rather than a token gesture.
    assert sum(gemini.PATIENT_BACKOFF) > 120


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
