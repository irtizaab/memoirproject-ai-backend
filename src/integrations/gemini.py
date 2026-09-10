# Integrations = thin wrappers around external services. This one is Google's
# Gemini API, which turns a prompt into a typed JSON object.
#
# Nothing in here knows what this product is. It takes a string and a Pydantic
# class and gives back an instance of that class. Every decision about what to
# ask, what the rules are, and what to do with the answer belongs in domain/.
#
# ---------------------------------------------------------------------------
# Why the REST endpoint rather than the google-genai SDK
#
# The SDK's client is synchronous by default, and a synchronous HTTP call
# inside an `async def` handler blocks the event loop for its whole duration —
# every other request this worker is serving waits behind it. That is the exact
# failure `test_no_blocking_io_in_src` exists to catch, and it does not show up
# as an error, only as latency under load.
#
# The SDK also brings its own connection pooling, its own retry policy and its
# own timeouts, none of which would be the ones configured in
# `integrations/http.py`. One request shape against one URL is small enough to
# write out, and this way it goes through the shared client like everything
# else here does.
# ---------------------------------------------------------------------------
#
# ---------------------------------------------------------------------------
# Why the schema is not optional
#
# `generate()` takes a Pydantic class and returns an instance of it, never a
# free string. Gemini is asked for `application/json` against a schema derived
# from that class, and the reply is validated before it is returned.
#
# The alternative — prose back, parsed by hand — fails in the worst possible
# way. A model that returns almost the right shape produces a value that looks
# fine in a log and is wrong in one field, and the caller finds out much later
# and somewhere else. A validation error here fails at the call, with the field
# named.
# ---------------------------------------------------------------------------

import asyncio
import logging
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from src.core.config import settings
from src.integrations.http import client

logger = logging.getLogger(__name__)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Statuses worth trying again, and nothing else.
#
# 503 UNAVAILABLE is the one that matters: Gemini returns it when the model is
# momentarily overloaded, and it arrives with no warning in the middle of an
# otherwise healthy run — six identical calls will fail one of them and serve
# the other five. 429 is rate limiting and 500 is their side falling over.
#
# 400 and 404 are not here on purpose. A malformed schema or a model name that
# does not exist will fail identically forever, and retrying it just makes the
# person wait three times as long for the same error.
_RETRY_ON = frozenset({429, 500, 503})

# How many extra attempts, and how long to wait before each.
#
# Short, and only two. This is a person watching a button, not a background
# job: a third retry is another two seconds of spinner for a failure that has
# already happened twice, and the screen recovers well — it says what went
# wrong and the standard questions are still there.
_BACKOFF_SECONDS = (1.0, 3.0)

T = TypeVar("T", bound=BaseModel)


class GeminiError(Exception):
    """Gemini could not do what was asked.

    Its own exception type, like `TranscriptionError` and `StorageError`, so a
    caller can tell "the upstream service failed" apart from a bug here.

    Every caller of this module is expected to catch it and carry on with
    something. Nothing this API does should fail because a model was
    unavailable.
    """


class GeminiDisabled(GeminiError):
    """No key, or the kill switch is off.

    A subclass rather than a separate type, so a caller that only wants to know
    "did this work" catches one thing — but distinguishable for the callers
    that want to stay quiet about a deliberately switched-off feature instead
    of logging an error about it.
    """


def _schema_for(model: type[BaseModel]) -> dict:
    """A response schema Gemini will accept, from a Pydantic class.

    Gemini's structured-output schema is a subset of OpenAPI and rejects some
    of what `model_json_schema()` emits. Two things have to go:

      `$defs` / `$ref`  — a nested model comes out as a reference into a
                          definitions block, which is not supported. The
                          inlining below flattens them.
      `additionalProperties`, `title`, `default` — ignored at best, a 400 at
                          worst, and none of them constrain the output.

    Everything else — types, enums, required, nesting, arrays — passes through,
    which is what makes the returned JSON worth validating rather than hoping
    about.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node):
        if isinstance(node, list):
            return [inline(item) for item in node]
        if not isinstance(node, dict):
            return node

        ref = node.get("$ref")
        if ref:
            # "#/$defs/PlannedBlock" -> the definition itself, inlined. Any
            # sibling keys on the reference (a description, usually) are kept.
            name = ref.rsplit("/", 1)[-1]
            resolved = dict(defs.get(name, {}))
            resolved.update({k: v for k, v in node.items() if k != "$ref"})
            return inline(resolved)

        return {
            key: inline(value)
            for key, value in node.items()
            if key not in ("additionalProperties", "title", "default")
        }

    return inline(schema)


async def _post(model: str, payload: dict, timeout: float) -> httpx.Response:
    """One request, retried while Gemini is momentarily unavailable.

    Returns the last response, successful or not — the caller decides what a
    4xx means. Raises `GeminiError` only when the request could not be made at
    all after every attempt.

    Retrying belongs here rather than in the callers because both of them want
    it and neither should have to know which status codes are transient. It
    does **not** belong in `integrations/http.py`: a retry on a POST is only
    safe because *this* endpoint is idempotent in the way that matters —
    generating text twice writes nothing twice — and that is not true of the
    other services that share the client.
    """
    last: httpx.Response | None = None

    for attempt in range(len(_BACKOFF_SECONDS) + 1):
        if attempt:
            await asyncio.sleep(_BACKOFF_SECONDS[attempt - 1])

        try:
            last = await (await client()).post(
                f"{_BASE_URL}/models/{model}:generateContent",
                # The key goes in a header, not in the query string. A URL
                # reaches access logs, proxy logs and error trackers; a header
                # does not.
                headers={"x-goog-api-key": settings.gemini_api_key},
                json=payload,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            # A timeout or a dropped connection. Worth one more try for the
            # same reason a 503 is, and the last one is re-raised as itself.
            if attempt == len(_BACKOFF_SECONDS):
                raise GeminiError(f"could not reach Gemini: {exc}") from exc
            logger.info("Retrying Gemini after a transport error: %s", exc)
            continue

        if last.status_code not in _RETRY_ON:
            return last

        logger.info(
            "Gemini answered %s; retrying (attempt %s)",
            last.status_code,
            attempt + 1,
        )

    return last


async def generate(
    prompt: str,
    schema: type[T],
    *,
    system: str | None = None,
    model: str | None = None,
    temperature: float = 0.4,
    timeout: float = 60.0,
) -> T:
    """Ask Gemini a question and get back a validated `schema` instance.

    `system` carries the rules — who the model is and what it may not do — and
    `prompt` carries the material. Kept apart because Gemini treats a system
    instruction as standing context rather than as the latest turn, so rules
    put there survive a long prompt better than rules buried above one.

    `temperature` defaults low. Everything this app asks for is closer to
    extraction than to invention, and a high temperature on a task like that
    does not produce better writing, only less predictable structure.

    Raises `GeminiDisabled` when there is no key or the switch is off, and
    `GeminiError` for everything else: unreachable, refused, empty, or a reply
    that did not match the schema.
    """
    if not settings.ai_enabled:
        raise GeminiDisabled("AI_ENABLED is false")
    if not settings.gemini_api_key:
        raise GeminiDisabled("GEMINI_API_KEY is not set")

    chosen = model or settings.gemini_model

    payload: dict[str, object] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
            "responseSchema": _schema_for(schema),
        },
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    response = await _post(chosen, payload, timeout)

    if response.status_code >= 400:
        # The prompt is not logged. It carries what people wrote about someone
        # they have lost, and a log file is the wrong place for that.
        logger.error("Gemini refused the request: %s", response.status_code)
        raise GeminiError(f"Gemini refused the request ({response.status_code})")

    body = response.json()

    candidates = body.get("candidates") or []
    if not candidates:
        # No candidate at all means the *prompt* was blocked, which is a
        # different failure from a truncated answer and worth naming.
        reason = (body.get("promptFeedback") or {}).get("blockReason")
        raise GeminiError(f"Gemini returned nothing (blockReason={reason})")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()

    if not text:
        # MAX_TOKENS here means the JSON is cut off mid-object, so there is
        # nothing to salvage; SAFETY means it was stopped. Either way the
        # finish reason is the only useful thing to say about it.
        raise GeminiError(
            f"Gemini returned an empty answer "
            f"(finishReason={candidate.get('finishReason')})"
        )

    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        # The count, not the content. The text is the model's answer about
        # someone's life and does not belong in a log either.
        logger.error(
            "Gemini returned JSON that did not match %s: %s problems",
            schema.__name__,
            len(exc.errors()),
        )
        raise GeminiError(f"Gemini returned a malformed {schema.__name__}") from exc
