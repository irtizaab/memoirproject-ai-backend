# Integrations = thin wrappers around external services. This one is
# AssemblyAI, which turns a recording into words.
#
# Nothing in here knows what a memoir is. It takes a URL to some audio and
# gives back a job id, or takes a job id and gives back what the job produced.
# The rules about which recordings get transcribed, and who may read the
# result, belong in domain/transcripts/.
#
# ---------------------------------------------------------------------------
# Why we send a URL rather than the audio
#
# AssemblyAI will fetch the file itself if given an address it can reach. The
# bucket is private, but `create_signed_download_url` already mints temporary
# read URLs for exactly this kind of purpose — that is how an <audio> tag plays
# a private object.
#
# So the bytes go: browser -> Supabase Storage -> AssemblyAI. They never pass
# through this API in either direction. Uploading them here first would mean
# downloading 14 MB from storage and posting it straight back out again, on a
# worker, for every voice note.
# ---------------------------------------------------------------------------
#
# ---------------------------------------------------------------------------
# Why we do not ask for word-level timings
#
# The response can include a `words` array with start, end and confidence for
# every word — around 9,000 objects, some 750 KB of JSON, for one hour of
# speech. That is fifteen times the size of the transcript itself.
#
# Paragraphs give the same useful thing (jump to this part of the recording)
# for a fraction of it, so `paragraphs()` below is what the domain layer reads
# and the word array is left on the floor.
# ---------------------------------------------------------------------------

import logging

import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.assemblyai.com/v2"

# The header AssemblyAI is asked to send back with the webhook. Paired with a
# secret we invent; see `settings.assemblyai_webhook_secret`.
WEBHOOK_AUTH_HEADER = "X-Memoir-Webhook-Secret"


class TranscriptionError(Exception):
    """AssemblyAI could not do what was asked.

    Its own exception type, like `StorageError`, so a caller can tell "the
    upstream service failed" apart from a bug here. Nothing catching this
    should treat it as fatal: a recording without a transcript is still a
    recording, and the archive renders it perfectly well.
    """


def _headers() -> dict[str, str]:
    """Authorization for the AssemblyAI API.

    Raises rather than returning a half-formed header, so a missing key fails
    at the point of use with a message naming the variable, instead of arriving
    later as an opaque 401 from someone else's server.
    """
    if not settings.assemblyai_api_key:
        raise TranscriptionError(
            "ASSEMBLYAI_API_KEY is not set — recordings cannot be "
            "transcribed. Add it to .env."
        )
    return {"Authorization": settings.assemblyai_api_key}


def submit(audio_url: str) -> dict:
    """Start a transcription job. Returns `{"id": ..., "status": ...}`.

    Asynchronous by nature: the reply arrives immediately with a job id and
    `status: "queued"`, and the actual work takes roughly a tenth of the
    recording's length. Nothing here waits for it.

    `language_detection` is on rather than a hardcoded language. The people
    these memoirs are about do not all speak English, and asking for `en`
    transcription of Urdu or Punjabi does not fail — it returns fluent,
    confident nonsense, which is far worse than failing.

    A webhook is requested only when there is somewhere to send it. On a laptop
    there is not, and the reconcile-on-read path in the domain layer collects
    the result instead.
    """
    payload: dict[str, object] = {
        "audio_url": audio_url,
        "language_detection": True,
        # Both default to true, but a memoir is prose someone will read, and
        # relying on a provider default for that is how a punctuation-free wall
        # of text ships one day without warning.
        "punctuate": True,
        "format_text": True,
    }

    if settings.assemblyai_speech_model:
        payload["speech_model"] = settings.assemblyai_speech_model

    webhook_url = settings.assemblyai_webhook_url
    if webhook_url:
        payload["webhook_url"] = webhook_url
        payload["webhook_auth_header_name"] = WEBHOOK_AUTH_HEADER
        payload["webhook_auth_header_value"] = settings.assemblyai_webhook_secret

    try:
        response = httpx.post(
            f"{_BASE_URL}/transcript",
            headers=_headers(),
            json=payload,
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"could not reach AssemblyAI: {exc}") from exc

    if response.status_code >= 400:
        # The URL is not logged: it is a signed URL, and a signed URL in a log
        # file is a readable copy of somebody's recording for as long as the
        # signature lasts.
        logger.error(
            "Submitting a transcription failed: %s %s",
            response.status_code,
            response.text,
        )
        raise TranscriptionError(
            f"AssemblyAI refused the job ({response.status_code})"
        )

    body = response.json()
    if not body.get("id"):
        raise TranscriptionError("AssemblyAI returned no job id")

    return body


def fetch(provider_id: str) -> dict:
    """Ask what a job has produced, if anything yet.

    The poll half of the two ways a result arrives. Returns the job as-is,
    including `status`, which the caller inspects — this function has no
    opinion about whether "processing" is interesting.
    """
    try:
        response = httpx.get(
            f"{_BASE_URL}/transcript/{provider_id}",
            headers=_headers(),
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"could not reach AssemblyAI: {exc}") from exc

    if response.status_code >= 400:
        raise TranscriptionError(
            f"AssemblyAI would not return job {provider_id} "
            f"({response.status_code})"
        )

    return response.json()


def paragraphs(provider_id: str) -> list[dict]:
    """The transcript split into paragraphs, each with a start and end.

    A separate endpoint from the transcript itself, and a separate call. Worth
    it: this is the entire reason a transcript can later be clicked to seek,
    and it is a twentieth the size of asking for word-level timings.

    Returns `[]` rather than raising if AssemblyAI cannot produce them. A
    transcript with no paragraph breaks is still a transcript, and the reader
    falls back to the plain text.
    """
    try:
        response = httpx.get(
            f"{_BASE_URL}/transcript/{provider_id}/paragraphs",
            headers=_headers(),
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        logger.warning("Could not fetch paragraphs for %s: %s", provider_id, exc)
        return []

    if response.status_code >= 400:
        logger.warning(
            "Could not fetch paragraphs for %s: %s",
            provider_id,
            response.status_code,
        )
        return []

    return response.json().get("paragraphs") or []
