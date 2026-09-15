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
import base64
import logging
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from src.core.config import settings
from src.integrations.http import client

logger = logging.getLogger(__name__)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# The second provider. Groq speaks OpenAI's chat-completions shape, so it is
# a different payload against a different URL and the same retry, the same
# schema rule and the same errors. A model is sent there when its name is
# written `groq:<model>` in settings — one prefix, no provider switch, and a
# deployment can put the builder on one and the guide on the other.
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_PREFIX = "groq:"

# Groq's vision models look at five images per request and no more. Past that
# the rest are not sent — logged, and still placed by the caller's fallback
# rule — where Gemini reads the whole archive.
_GROQ_MAX_IMAGES = 5

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
# Short, and only two, for the follow-up questions: a person is watching a
# button, a third retry is another two seconds of spinner for a failure that
# has already happened twice, and that screen recovers well — it says what went
# wrong and the standard questions are still there.
_BACKOFF_SECONDS = (1.0, 3.0)

# The other kind of call, and the reason `generate` takes a schedule at all.
#
# Assembly runs once, by hand, over a whole archive, with a 300-second timeout
# because the owner pressed a button and expects to wait. Giving up on a
# transient 503 after four seconds of waiting spends none of that budget and
# costs a family a book organised by decade instead of by chapter — which is
# exactly what happened: `gemini-3.5-flash` answered "this model is currently
# experiencing high demand" three times in seventeen seconds, and the fallback
# ran while five minutes of patience were still unspent.
#
# Two and a half minutes of retrying, inside a five-minute budget, leaves room
# for the call itself to take the two it usually takes.
PATIENT_BACKOFF = (5.0, 15.0, 40.0, 90.0)

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

    Gemini's structured-output schema is a subset of OpenAPI 3.0 and rejects
    some of what `model_json_schema()` emits. Three things have to change:

      `$defs` / `$ref`  — a nested model comes out as a reference into a
                          definitions block, which is not supported. The
                          inlining below flattens them.
      `additionalProperties`, `title`, `default` — ignored at best, a 400 at
                          worst, and none of them constrain the output. As
                          *keywords* only: a field of that name inside
                          `properties` is left alone.
      `anyOf` with a null branch — how Pydantic spells `str | None`, and how
                          OpenAPI 3.0 does not. `_denullify` rewrites it as
                          `nullable: true` on the remaining branch.

    That last one is not a nicety. Gemini answers **400** to a schema
    containing `{"type": "null"}`, the whole request fails, and
    `planner.plan()` returns None — so a model with one optional field in its
    response shape silently becomes "the AI is switched off". That is exactly
    what happened to assembly: `Plan` has three optional fields (`quote`,
    `from_year`, `through_year`), so every memoir was organised by the decade
    fallback, while `Library` — which has none — worked, which is what made it
    look like a feature problem rather than a schema one.

    Everything else — types, enums, required, nesting, arrays — passes through,
    which is what makes the returned JSON worth validating rather than hoping
    about.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def denullify(node: dict) -> dict:
        """`anyOf: [X, {"type": "null"}]` -> X with `nullable: true`.

        Only touches the two-branch optional shape, which is the one Pydantic
        emits for `X | None`. A genuine union of two real types is left alone:
        it would need a judgement about which branch survives, and there is
        none in this codebase to make it for.
        """
        options = node.get("anyOf")
        if not isinstance(options, list):
            return node

        real = [o for o in options if not (isinstance(o, dict) and o.get("type") == "null")]
        if len(real) != 1 or len(real) == len(options):
            return node

        collapsed = dict(real[0])
        collapsed["nullable"] = True
        # Keep whatever sat beside the anyOf — the field's description, which
        # is where the prompt half of this schema actually lives.
        collapsed.update({k: v for k, v in node.items() if k != "anyOf"})
        return collapsed

    def inline(node):
        if isinstance(node, list):
            return [inline(item) for item in node]
        if not isinstance(node, dict):
            return node

        # Before the reference is resolved, not after: `Leaf | None` collapses
        # to the branch that holds the `$ref`, and a `$ref` that arrives here
        # already collapsed still has to be inlined like any other.
        node = denullify(node)

        ref = node.get("$ref")
        if ref:
            # "#/$defs/PlannedBlock" -> the definition itself, inlined. Any
            # sibling keys on the reference (a description, usually) are kept.
            name = ref.rsplit("/", 1)[-1]
            resolved = dict(defs.get(name, {}))
            resolved.update({k: v for k, v in node.items() if k != "$ref"})
            return inline(resolved)

        # `properties` is a mapping of field names, not of schema keywords,
        # so its keys are recursed into but never filtered. A field genuinely
        # called `title` — `PlannedChapter.title` is one — was otherwise
        # deleted from `properties` while `required` still named it, and Gemini
        # answers 400 to a required property that is not defined.
        return {
            key: (
                {name: inline(sub) for name, sub in value.items()}
                if key == "properties" and isinstance(value, dict)
                else inline(value)
            )
            for key, value in node.items()
            if key not in ("additionalProperties", "title", "default")
        }

    return inline(schema)


async def _post(
    url: str,
    headers: dict,
    payload: dict,
    timeout: float,
    backoff: tuple[float, ...] = _BACKOFF_SECONDS,
) -> httpx.Response:
    """One request, retried while the provider is momentarily unavailable.

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

    for attempt in range(len(backoff) + 1):
        if attempt:
            await asyncio.sleep(backoff[attempt - 1])

        try:
            # The key goes in a header, not in the query string. A URL
            # reaches access logs, proxy logs and error trackers; a header
            # does not.
            last = await (await client()).post(
                url, headers=headers, json=payload, timeout=timeout
            )
        except httpx.HTTPError as exc:
            # A timeout or a dropped connection. Worth one more try for the
            # same reason a 503 is, and the last one is re-raised as itself.
            if attempt == len(backoff):
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
    images: list[tuple[str, bytes]] | None = None,
    temperature: float = 0.4,
    timeout: float = 60.0,
    backoff: tuple[float, ...] = _BACKOFF_SECONDS,
    max_output_tokens: int | None = None,
) -> T:
    """Ask Gemini a question and get back a validated `schema` instance.

    `system` carries the rules — who the model is and what it may not do — and
    `prompt` carries the material. Kept apart because Gemini treats a system
    instruction as standing context rather than as the latest turn, so rules
    put there survive a long prompt better than rules buried above one.

    `images` are `(mime_type, bytes)` pairs sent as inline parts in the same
    turn as the prompt, so the model sees the pictures and the words together.
    They go *after* the text, because a part list is ordered and the prompt has
    to explain what the images are before there are any. Anything that needs
    to refer to a specific image must label it in the prompt — an inline part
    carries no id, only pixels.

    Nothing about an image is logged, on the same rule as the prompt and the
    reply: this module is where a family's private material is handed to
    somebody else's computer, and it should be the one place with nothing to
    read afterwards.

    `temperature` defaults low. Everything this app asks for is closer to
    extraction than to invention, and a high temperature on a task like that
    does not produce better writing, only less predictable structure.

    Raises `GeminiDisabled` when there is no key or the switch is off, and
    `GeminiError` for everything else: unreachable, refused, empty, or a reply
    that did not match the schema.
    """
    if not settings.ai_enabled:
        raise GeminiDisabled("AI_ENABLED is false")

    chosen = model or settings.gemini_model
    if chosen.startswith(_GROQ_PREFIX):
        if not settings.groq_api_key:
            raise GeminiDisabled("GROQ_API_KEY is not set")
        return await _generate_groq(
            chosen[len(_GROQ_PREFIX) :],
            prompt,
            schema,
            system=system,
            images=images,
            temperature=temperature,
            timeout=timeout,
            backoff=backoff,
            max_output_tokens=max_output_tokens,
        )
    if not settings.gemini_api_key:
        raise GeminiDisabled("GEMINI_API_KEY is not set")

    parts: list[dict] = [{"text": prompt}]
    for mime, raw in images or []:
        parts.append(
            {
                "inlineData": {
                    "mimeType": mime,
                    "data": base64.b64encode(raw).decode("ascii"),
                }
            }
        )

    config: dict[str, object] = {
        "temperature": temperature,
        "responseMimeType": "application/json",
        "responseSchema": _schema_for(schema),
    }
    if max_output_tokens is not None:
        # Unset means the model's default ceiling, which is fine for a
        # question and was not fine for a book: a plan carries every composed
        # paragraph *and* a quote per source, so the answer is longer than the
        # archive that produced it — and on the 3.x models the thinking tokens
        # come out of the same budget. Past the ceiling the reply is not an
        # error, it is a JSON document that stops mid-string.
        config["maxOutputTokens"] = max_output_tokens

    payload: dict[str, object] = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": config,
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    response = await _post(
        f"{_BASE_URL}/models/{chosen}:generateContent",
        {"x-goog-api-key": settings.gemini_api_key},
        payload,
        timeout,
        backoff,
    )

    if response.status_code >= 400:
        # The prompt is not logged. It carries what people wrote about someone
        # they have lost, and a log file is the wrong place for that.
        # The body is Google's own message about the request shape ("property
        # is not defined", a bad model name) and never carries any of what was
        # sent, so it is safe where the prompt and the reply are not. Without
        # it a schema bug is a bare 400, and the fallback makes it look like a
        # feature decision.
        logger.error(
            "Gemini refused the request: %s %s",
            response.status_code,
            response.text[:500],
        )
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

    # Truncation is not a malformed answer and must not be reported as one:
    # the model did what it was asked and ran out of room, so the remedy is a
    # bigger `max_output_tokens` or less material, not a different prompt.
    # Checked before parsing, because a document cut mid-string fails as
    # "json_invalid at the root", which says nothing about why.
    if candidate.get("finishReason") == "MAX_TOKENS":
        logger.error(
            "Gemini ran out of output tokens composing a %s", schema.__name__
        )
        raise GeminiError(
            f"Gemini's answer was cut off before it finished the "
            f"{schema.__name__} (MAX_TOKENS)"
        )

    return _validated(text, schema)


async def _generate_groq(
    model: str,
    prompt: str,
    schema: type[T],
    *,
    system: str | None,
    images: list[tuple[str, bytes]] | None,
    temperature: float,
    timeout: float,
    backoff: tuple[float, ...],
    max_output_tokens: int | None,
) -> T:
    """The same call, in OpenAI's chat shape, against Groq.

    The schema goes in `response_format` as JSON Schema proper — Pydantic's
    own output, `$defs` and `anyOf` included, none of the OpenAPI 3.0 rewriting
    Gemini needs. Images ride in the user turn as data URLs, first five only.
    """
    content: list[dict] = [{"type": "text", "text": prompt}]
    shown = (images or [])[:_GROQ_MAX_IMAGES]
    if len(images or []) > len(shown):
        logger.warning(
            "Groq looks at %d images per request; %d not sent",
            _GROQ_MAX_IMAGES,
            len(images) - len(shown),
        )
    for mime, raw in shown:
        data = base64.b64encode(raw).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
        )

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})

    payload: dict[str, object] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": schema.model_json_schema(),
            },
        },
    }
    if max_output_tokens is not None:
        payload["max_completion_tokens"] = max_output_tokens

    response = await _post(
        _GROQ_URL,
        {"Authorization": f"Bearer {settings.groq_api_key}"},
        payload,
        timeout,
        backoff,
    )

    if response.status_code >= 400:
        # Same rule as above: the body is the provider's message about the
        # request shape and never carries the prompt.
        logger.error(
            "Groq refused the request: %s %s",
            response.status_code,
            response.text[:500],
        )
        raise GeminiError(f"Groq refused the request ({response.status_code})")

    choices = response.json().get("choices") or []
    if not choices:
        raise GeminiError("Groq returned nothing")
    choice = choices[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    if choice.get("finish_reason") == "length":
        logger.error("Groq ran out of output tokens composing a %s", schema.__name__)
        raise GeminiError(
            f"Groq's answer was cut off before it finished the {schema.__name__}"
        )
    if not text:
        raise GeminiError(
            f"Groq returned an empty answer (finish_reason={choice.get('finish_reason')})"
        )
    return _validated(text, schema)


def _validated(text: str, schema: type[T]) -> T:
    """The reply as a `schema` instance, or a `GeminiError` naming the field."""
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        # Where and what kind, never the value. `loc` is a path of field names
        # and list indices and `type` is Pydantic's own vocabulary
        # ("string_type", "missing"); neither can carry a word the model wrote
        # about someone's life, which is why `msg` and `input` are left out.
        #
        # The count alone was not enough. "1 problems" was, for one real run,
        # everything that was known about why a family's memoir came back
        # divided by decade instead of by chapter.
        faults = "; ".join(
            f"{'.'.join(str(part) for part in problem['loc'])}: {problem['type']}"
            for problem in exc.errors()[:5]
        )
        logger.error(
            "The model returned JSON that did not match %s: %d problem(s) — %s",
            schema.__name__,
            len(exc.errors()),
            faults,
        )
        raise GeminiError(f"The model returned a malformed {schema.__name__}") from exc
