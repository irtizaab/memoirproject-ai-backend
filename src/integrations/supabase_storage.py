# Integrations = thin wrappers around external services. This one is Supabase
# Storage, where the audio and photographs actually live.
#
# Nothing in here knows what a memoir is. It takes a path and gives back a URL.
# The rules about who may upload what, and where a path comes from, belong in
# domain/media/.
#
# ---------------------------------------------------------------------------
# Why signed URLs, rather than the file passing through this API
#
# The obvious design — browser POSTs the file here, this app forwards it to
# storage — is worse in three ways. It doubles the bandwidth, it ties up a
# worker for the length of a phone upload on a bad connection, and it puts a
# 50 MB request body limit problem in front of a grandmother recording a voice
# note.
#
# Instead: the browser asks for permission, gets a URL that works once, and
# uploads directly to storage. This app never sees the bytes.
# ---------------------------------------------------------------------------

import logging

import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Storage could not do what was asked.

    Its own exception type so the API layer can answer 502 — "the upstream
    service failed" — rather than letting an httpx error surface as a 500 that
    reads as a bug in this app.
    """


def _headers() -> dict[str, str]:
    """Authorization for the Storage API.

    The service role key bypasses Row Level Security, which is exactly why it
    is confined to this module: nothing above it can accidentally acquire the
    ability to read any object in the project.
    """
    if not settings.supabase_service_role_key:
        raise StorageError(
            "SUPABASE_SERVICE_ROLE_KEY is not set — media uploads cannot be "
            "signed. Add it to .env (Supabase → Settings → API)."
        )

    key = settings.supabase_service_role_key
    return {"Authorization": f"Bearer {key}", "apikey": key}


def create_signed_upload_url(path: str) -> str:
    """Mint a one-shot URL the browser can PUT a file to.

    Returns an absolute URL. The token inside it is scoped to this exact path,
    so a client cannot take a URL issued for its own upload and write over
    somebody else's object by editing the path.

    The reply from Supabase is a *relative* url, e.g.
    `/object/upload/sign/memoir-media/abc.webm?token=...`, which is why it gets
    joined onto the storage base here rather than returned as-is.
    """
    bucket = settings.supabase_storage_bucket
    url = f"{settings.supabase_storage_url}/object/upload/sign/{bucket}/{path}"

    try:
        response = httpx.post(url, headers=_headers(), timeout=15.0)
    except httpx.HTTPError as exc:
        raise StorageError(f"could not reach storage: {exc}") from exc

    if response.status_code >= 400:
        logger.error(
            "Signing upload failed: %s %s", response.status_code, response.text
        )
        raise StorageError(f"storage refused to sign the upload ({response.status_code})")

    signed = response.json().get("url")
    if not signed:
        raise StorageError("storage returned no signed url")

    return f"{settings.supabase_storage_url}{signed}"


def create_signed_download_url(path: str) -> str:
    """Mint a temporary read URL for a private object.

    The bucket is private, so an object has no public address. This is how a
    photograph reaches an `<img>` tag and a voice note reaches an `<audio>`
    tag without the bucket being open to the world.

    Expiring matters: these URLs end up in HTML, browser history and shared
    screenshots, and a permanent one would be a permanent leak.
    """
    bucket = settings.supabase_storage_bucket
    url = f"{settings.supabase_storage_url}/object/sign/{bucket}/{path}"

    try:
        response = httpx.post(
            url,
            headers=_headers(),
            json={"expiresIn": settings.signed_url_ttl_seconds},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise StorageError(f"could not reach storage: {exc}") from exc

    if response.status_code >= 400:
        logger.error(
            "Signing download failed: %s %s", response.status_code, response.text
        )
        raise StorageError(f"storage refused to sign the download ({response.status_code})")

    signed = response.json().get("signedURL")
    if not signed:
        raise StorageError("storage returned no signed url")

    return f"{settings.supabase_storage_url}{signed}"


def create_signed_download_urls(paths: list[str]) -> dict[str, str]:
    """Sign many objects at once, returning `{path: url}`.

    The archive shows a dozen memories at a time, each with a photograph or a
    voice note. Calling `create_signed_download_url` per asset would put a
    dozen sequential HTTP round-trips inside one page render; Supabase signs a
    batch in one, so that is what the list endpoints use.

    Paths that cannot be signed are simply absent from the result rather than
    raising. One missing photograph should render as a gap, not take down the
    whole archive with a 502.
    """
    if not paths:
        return {}

    bucket = settings.supabase_storage_bucket
    url = f"{settings.supabase_storage_url}/object/sign/{bucket}"

    try:
        response = httpx.post(
            url,
            headers=_headers(),
            json={"expiresIn": settings.signed_url_ttl_seconds, "paths": paths},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        logger.warning("Batch signing failed: %s", exc)
        return {}

    if response.status_code >= 400:
        logger.warning(
            "Batch signing failed: %s %s", response.status_code, response.text
        )
        return {}

    signed: dict[str, str] = {}
    for entry in response.json():
        # Supabase returns the path it was given plus a relative signedURL, and
        # an `error` string on the ones it could not sign.
        path = entry.get("path")
        relative = entry.get("signedURL")
        if path and relative:
            signed[path] = f"{settings.supabase_storage_url}{relative}"

    return signed


def object_size(path: str) -> int:
    """How many bytes storage is actually holding at `path`.

    Asked rather than accepted. The client knows the size of the file it sent
    and could simply tell us, but then the storage meter — and any future
    quota built on it — would be a number the client chooses. This is the only
    honest source.

    Raises StorageError if the object is not there, which is the useful
    outcome: it means the confirm step arrived without the upload succeeding,
    and the asset should not be marked complete.
    """
    bucket = settings.supabase_storage_bucket
    url = f"{settings.supabase_storage_url}/object/{bucket}/{path}"

    try:
        response = httpx.head(url, headers=_headers(), timeout=15.0)
    except httpx.HTTPError as exc:
        raise StorageError(f"could not reach storage: {exc}") from exc

    if response.status_code >= 400:
        raise StorageError(f"no object at that path ({response.status_code})")

    length = response.headers.get("content-length")
    if length is None:
        raise StorageError("storage did not report a size")

    return int(length)


def delete_object(path: str) -> None:
    """Remove an object. Used when its memory is deleted.

    A failure here is logged and swallowed rather than raised. The caller has
    already removed the database row inside a committed transaction; blowing up
    afterwards would report failure for work that succeeded, and leave the user
    unable to delete a memory because of a hiccup in a different system. The
    cost of the alternative is an orphaned object, which costs storage and
    nothing else.
    """
    bucket = settings.supabase_storage_bucket
    url = f"{settings.supabase_storage_url}/object/{bucket}/{path}"

    try:
        response = httpx.delete(url, headers=_headers(), timeout=15.0)
        if response.status_code >= 400:
            logger.warning(
                "Could not delete %s: %s %s", path, response.status_code, response.text
            )
    except httpx.HTTPError as exc:
        logger.warning("Could not delete %s: %s", path, exc)
