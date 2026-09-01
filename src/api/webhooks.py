# API layer = FastAPI routes. Callbacks from services we handed work to.
#
# One endpoint so far: AssemblyAI telling us a transcription has finished.
#
# ---------------------------------------------------------------------------
# Why this route is public, and how it is protected anyway
#
# AssemblyAI has no account here and can hold no Supabase token, so there is no
# credential of the usual kind for it to send. Instead, every job is submitted
# with a header name and a value we invent (see `settings.assemblyai_webhook_
# secret`), AssemblyAI echoes both back on the callback, and this route checks
# the value matches.
#
# That makes the secret the entire authentication for this endpoint, which is
# why it is compared with `hmac.compare_digest` rather than `==`. A plain string
# comparison returns as soon as two bytes differ, and the time it takes leaks
# how much of the secret a guess got right.
# ---------------------------------------------------------------------------

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from src.core.config import settings
from src.domain.transcripts.transcript_service import apply_result
from src.integrations.assemblyai import WEBHOOK_AUTH_HEADER

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _authentic(supplied: str | None) -> bool:
    """Whether this callback carries the secret we issued with the job."""
    expected = settings.assemblyai_webhook_secret
    if not expected or not supplied:
        return False
    return hmac.compare_digest(supplied, expected)


@router.post("/assemblyai", status_code=200)
async def post_assemblyai(
    request: Request,
    secret: str | None = Header(default=None, alias=WEBHOOK_AUTH_HEADER),
):
    """AssemblyAI reporting that a transcript is ready, or that it failed.

    Returns 200 for very nearly everything, on purpose. A webhook sender reads
    a non-2xx as "try again later" and will keep retrying on a schedule, so an
    endpoint that answers 500 to a payload it does not like arranges to be sent
    that payload repeatedly for hours. The only thing worth refusing is a
    request that cannot prove where it came from.

    An unrecognised job id is therefore a 200 as well. It means the callback is
    genuine but the row is gone — the memory was deleted while the transcript
    was still being made, which is ordinary — and there is nothing to retry.

    `async def` here, unlike the rest of this codebase: reading the raw body
    off the request is awaitable, and there is no blocking database call on the
    path that would need a threadpool.
    """
    if not _authentic(secret):
        logger.warning("Rejected an AssemblyAI callback with a bad secret")
        raise HTTPException(status_code=401, detail="not authenticated")

    try:
        payload = await request.json()
    except ValueError:
        logger.warning("AssemblyAI callback carried no readable JSON")
        return {"received": False}

    provider_id = payload.get("transcript_id") or payload.get("id")
    if not provider_id:
        logger.warning("AssemblyAI callback named no transcript")
        return {"received": False}

    # The callback body tells us the status but not the text, so the result is
    # fetched rather than trusted. `apply_result` does that itself when the
    # payload is thin — see the status handling there.
    status = payload.get("status")
    if status not in ("completed", "error"):
        return {"received": True}

    applied = apply_result(provider_id, payload)
    if not applied:
        # Genuine callback, no row. Deleted mid-flight, or a job from another
        # environment pointed at this URL. Neither is worth a retry.
        logger.info("AssemblyAI callback for unknown job %s", provider_id)

    return {"received": True}
