# API layer = FastAPI routes. Uploads.
#
# These two endpoints accept either credential, because both people who can add
# a memory can add a photograph to it: the owner with a bearer token, and a
# contributor with a share link. Neither is preferred — the handler passes on
# whatever arrived and the domain layer decides whether it is enough.

import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.domain.media.media_service import (
    UnsupportedMediaType,
    UploadNotConfirmed,
    begin_upload,
    complete_upload,
)
from src.domain.transcripts.transcript_service import request_transcription
from src.integrations.supabase_auth import TokenError, verify_access_token
from src.integrations.supabase_storage import StorageError
from src.models.memory_models import MediaAsset, UploadRequest, UploadTicket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/media", tags=["media"])

# `auto_error=False` so a missing header returns None instead of raising. These
# routes genuinely accept no bearer token — a contributor has none — so the
# absence has to be a value the handler can inspect rather than an error.
_bearer = HTTPBearer(auto_error=False, description="Supabase access token")


def optional_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str | None:
    """The caller's id if they sent a valid token, otherwise None.

    Distinct from `current_user`, which 401s. Here, "no token" is a legitimate
    way to call the endpoint, so it must not be an error — the link token is
    checked instead, further down.

    An invalid token is also None rather than a 401. It reaches the same place
    a missing one does: the request is judged on whatever link token came with
    it, and fails with 404 if that is not good enough either.
    """
    if credentials is None or not credentials.credentials:
        return None

    try:
        return verify_access_token(credentials.credentials)["sub"]
    except TokenError:
        logger.info("Upload request carried an unusable bearer token")
        return None


@router.post("/uploads", response_model=UploadTicket, status_code=201)
def post_upload(
    body: UploadRequest,
    user_id: str | None = Depends(optional_user_id),
    x_link_token: str | None = Header(
        default=None, description="share link token, for contributors"
    ),
):
    """Reserve somewhere for a file and get a one-shot URL to PUT it to.

    The browser uploads directly to storage from here; the bytes never pass
    through this API. See `domain/media/media_service.py` for why.

    Two failure modes worth telling apart. A mime type outside the allow-list
    is the client's mistake and gets a 400 naming it — `image/svg+xml` and
    `text/html` are refused because a stored file is served back to browsers,
    and those two execute. Storage being unreachable is not the client's fault
    and gets a 502.
    """
    if user_id is None and x_link_token is None:
        raise HTTPException(
            status_code=401,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        ticket = begin_upload(
            memoir_id=str(body.memoir_id),
            kind=body.kind,
            mime_type=body.mime_type,
            original_filename=body.original_filename,
            duration_ms=body.duration_ms,
            user_id=user_id,
            link_token=x_link_token,
        )
    except UnsupportedMediaType as exc:
        raise HTTPException(
            status_code=400, detail=f"{exc} files are not accepted here"
        )
    except StorageError as exc:
        logger.error("Could not sign an upload: %s", exc)
        raise HTTPException(status_code=502, detail="storage is unavailable")

    if ticket is None:
        raise HTTPException(status_code=404, detail="memoir not found")
    return ticket


@router.post("/uploads/{asset_id}/complete", response_model=MediaAsset)
def post_upload_complete(
    asset_id: UUID,
    background_tasks: BackgroundTasks,
    user_id: str | None = Depends(optional_user_id),
    x_link_token: str | None = Header(
        default=None, description="share link token, for contributors"
    ),
):
    """Confirm the file arrived, and record the size storage reports.

    Called after the PUT succeeds. Until it is, the asset is a reservation: it
    holds a path, counts towards nobody's storage, and cannot be attached to a
    memory.

    A confirmed recording is also where transcription starts — see the bottom
    of this function. This is the right moment for it because it is the first
    point at which the audio is known to exist in storage, and it is before the
    memory itself is created, so the words are often ready by the time anyone
    looks for them.
    """
    if user_id is None and x_link_token is None:
        raise HTTPException(
            status_code=401,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        asset = complete_upload(
            str(asset_id), user_id=user_id, link_token=x_link_token
        )
    except UploadNotConfirmed:
        raise HTTPException(
            status_code=409, detail="no file was uploaded to that address"
        )
    except StorageError as exc:
        # Storage answering "no such object" arrives as a StorageError from
        # object_size(). It means the PUT never landed, which is the client's
        # problem to retry, not a server fault — hence 409 rather than 502.
        logger.info("Confirm failed for asset %s: %s", asset_id, exc)
        raise HTTPException(
            status_code=409, detail="no file was uploaded to that address"
        )

    if asset is None:
        raise HTTPException(status_code=404, detail="upload not found")

    # After the response, not before it. A background task lets confirming an
    # upload stay as fast as it is today while the (much slower) business of
    # signing a URL and talking to AssemblyAI happens behind it.
    #
    # `request_transcription` never raises, so nothing it does can surface as a
    # failed confirmation of an upload that in fact succeeded.
    if asset["kind"] == "audio":
        background_tasks.add_task(request_transcription, str(asset_id))

    return asset
