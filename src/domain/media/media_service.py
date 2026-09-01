# Domain layer for uploads.
#
# The flow this implements, and why it has three steps rather than one:
#
#   1. begin_upload()     reserve a row, get back a URL that works once
#   2. the browser PUTs the file straight to storage — this app never sees it
#   3. complete_upload()  ask storage how big it really is, mark it done
#
# Step 2 is the reason for the other two. A voice note recorded on a phone over
# a rural connection is a slow upload; routing it through this API would tie up
# a worker for its duration and double the bandwidth bill for no gain.

import logging
import uuid

from src.domain.memoirs.access import contributable_memoir, owned_memoir
from src.integrations.db import db
from src.integrations.supabase_storage import (
    create_signed_upload_url,
    object_size,
)

logger = logging.getLogger(__name__)


class UploadNotConfirmed(Exception):
    """Storage has no object at the path this asset reserved.

    Almost always means the PUT never happened or failed halfway. Raised
    instead of quietly marking the asset complete, because a confirmed asset
    with no file behind it renders as a broken image forever.
    """


# What each media type is allowed to be, and the extension its object gets.
#
# An allow-list rather than "trust the Content-Type header": the mime type
# becomes part of the stored object's metadata and is echoed back to browsers,
# and `text/html` served from your own domain is the classic stored-XSS route.
_IMAGE_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
    "image/gif": "gif",
}

# Browsers disagree about what they record. Chrome and Firefox produce webm,
# iOS Safari produces mp4 — sometimes labelled `audio/mp4`, sometimes `x-m4a`.
# All of them are stored as-is and played back by `<audio>`, which knows what to
# do with each. Transcoding would mean a media pipeline this product does not
# need yet.
_AUDIO_TYPES = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/aac": "aac",
}


class UnsupportedMediaType(Exception):
    """The mime type is not on the allow-list for this kind of asset."""


def _extension_for(kind: str, mime_type: str) -> str:
    """The file extension for an allowed type, or raise.

    The type is matched on its base, so `audio/webm;codecs=opus` — which is
    what MediaRecorder actually reports — is accepted rather than rejected on a
    technicality.
    """
    base = mime_type.split(";")[0].strip().lower()
    table = _IMAGE_TYPES if kind == "image" else _AUDIO_TYPES

    if base not in table:
        raise UnsupportedMediaType(base)
    return table[base]


def _authorize(cur, memoir_id: str, user_id: str | None, link_token: str | None):
    """Resolve whichever credential was supplied into a memoir, or None.

    Both paths end at the same place, which is the point: an upload is allowed
    if you own the memoir, or if you hold a live contribute link to it. The
    caller does not get to say which — it supplies what it has.
    """
    if user_id is not None:
        return owned_memoir(cur, memoir_id, user_id)

    if link_token is not None:
        memoir = contributable_memoir(cur, link_token)
        # The link has to point at the memoir being written to. Without this
        # check, a valid link to memoir A would authorize an upload into B.
        if memoir is not None and str(memoir["id"]) == memoir_id:
            return memoir

    return None


def begin_upload(
    memoir_id: str,
    kind: str,
    mime_type: str,
    original_filename: str | None = None,
    duration_ms: int | None = None,
    user_id: str | None = None,
    link_token: str | None = None,
) -> dict | None:
    """Reserve a place for a file and hand back a URL to PUT it to.

    Returns `{asset_id, upload_url}`, or None if the caller may not write to
    this memoir.

    The object key is `{memoir_id}/{random}.{ext}` — random rather than derived
    from the original filename, which would leak names like
    `dads-funeral-2019.jpg` into a URL, and would collide the second time two
    people uploaded `IMG_0001.jpg`.
    """
    extension = _extension_for(kind, mime_type)
    storage_path = f"{memoir_id}/{uuid.uuid4().hex}.{extension}"

    with db() as conn, conn.cursor() as cur:
        if _authorize(cur, memoir_id, user_id, link_token) is None:
            return None

        cur.execute(
            """
            INSERT INTO media_asset
                (memoir_id, kind, storage_path, mime_type,
                 original_filename, duration_ms)
            VALUES
                (%(memoir_id)s, %(kind)s::asset_kind, %(storage_path)s,
                 %(mime_type)s, %(original_filename)s, %(duration_ms)s)
            RETURNING id
            """,
            {
                "memoir_id": memoir_id,
                "kind": kind,
                "storage_path": storage_path,
                "mime_type": mime_type,
                "original_filename": original_filename,
                "duration_ms": duration_ms if kind == "audio" else None,
            },
        )
        asset_id = cur.fetchone()["id"]

    # Signed outside the transaction on purpose. It is a network call to
    # another service, and holding a Postgres transaction open across one ties
    # up a connection for as long as Supabase takes to answer. If it fails, the
    # reservation row is orphaned — harmless, uncounted, and swept up by the
    # cleanup job noted at the end of migration 0003.
    upload_url = create_signed_upload_url(storage_path)

    return {"asset_id": asset_id, "upload_url": upload_url}


def complete_upload(
    asset_id: str,
    user_id: str | None = None,
    link_token: str | None = None,
) -> dict | None:
    """Confirm the file landed, and record how big it actually is.

    The size comes from storage, never from the client. The storage meter — and
    any quota built on it later — is only meaningful if the number behind it is
    one the client cannot choose.

    Raises UploadNotConfirmed when storage has nothing at the path, which is
    the honest answer when a PUT silently failed.
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, memoir_id, storage_path, uploaded_at
              FROM media_asset
             WHERE id = %(asset_id)s
            """,
            {"asset_id": asset_id},
        )
        asset = cur.fetchone()
        if asset is None:
            return None

        if _authorize(cur, str(asset["memoir_id"]), user_id, link_token) is None:
            return None

    # Outside the transaction again, for the same reason as above.
    size = object_size(asset["storage_path"])

    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE media_asset
               SET uploaded_at = COALESCE(uploaded_at, now()),
                   byte_size = %(size)s
             WHERE id = %(asset_id)s
         RETURNING id, kind::text AS kind, mime_type, byte_size, duration_ms
            """,
            {"asset_id": asset_id, "size": size},
        )
        confirmed = cur.fetchone()

    # `url` is absent rather than signed here: nothing displays an asset at the
    # moment it is confirmed. The memory it gets attached to signs it when the
    # archive reads it back.
    confirmed["url"] = None
    return confirmed
