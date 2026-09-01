# Domain layer for turning recordings into words.
#
# The flow, and why it has the shape it does:
#
#   1. an audio upload is confirmed
#   2. request_transcription()  signs a read URL, submits it, stores the job id
#   3. ...minutes pass...
#   4. apply_result()           writes the finished text through
#
# Step 4 has TWO callers and one implementation, which is the important design
# decision in this file. AssemblyAI's webhook calls it when the job finishes;
# `reconcile()` calls it when somebody reads a transcript that is still in
# flight. Two write paths would drift — one would learn about a new field, or
# a new failure mode, and the other would not. One function cannot.
#
# The webhook is the fast path and the reconcile is the safety net, and neither
# is required for correctness. On a laptop there is no public URL, so the
# webhook never fires and reconcile does all the work. In production the
# webhook almost always wins and reconcile fires approximately never — but it
# is still there for the webhook that gets lost, which happens.

import json
import logging

# `Jsonb`, not `Json`. The column is jsonb, and the plain adapter sends the
# `json` type — which Postgres will not COALESCE against a jsonb column.
from psycopg.types.json import Jsonb

from src.core.config import settings
from src.integrations.assemblyai import (
    TranscriptionError,
    fetch,
    paragraphs,
    submit,
)
from src.integrations.db import db
from src.integrations.supabase_storage import (
    StorageError,
    create_signed_download_url,
)

logger = logging.getLogger(__name__)

# How long a job may sit in 'processing' before a read is willing to spend an
# HTTP call checking on it. Short enough to feel immediate on a laptop, long
# enough that the webhook wins the race in production.
_RECONCILE_AFTER_SECONDS = 8

# At most this many jobs are chased per request. A memoir has one or two
# recordings in flight at a time in practice; the bound is here so that an
# unusual archive cannot turn one page load into thirty outbound calls.
_RECONCILE_LIMIT = 3

# Statuses that will never change again. Everything else is worth re-reading.
TERMINAL = ("done", "failed", "skipped")


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


def _over_budget(memoir_id: str, incoming_ms: int) -> bool:
    """Whether transcribing this recording would exceed the memoir's allowance.

    The allowance lives on the plan the memoir's owner is on
    (`plan.transcription_minutes`), because it is part of what they pay for
    rather than a property of the memoir itself.

    Consumption is **summed from what actually happened**, not tracked in a
    counter — the same discipline as the storage meter, and for the same
    reason: a counter has to stay correct across a failed job, a deleted
    memory and a webhook that arrived twice, and eventually it does not.

    And it prefers `transcript.audio_seconds`, which the provider reports,
    over `media_asset.duration_ms`, which the *client* sends. A budget summed
    from a number the limited party chooses is not a budget. The client's
    estimate is still used for admission, because the true length is not known
    until a job finishes — so someone who understates a duration gets one
    recording through and then meets a ledger that has corrected itself.

    Only 'processing' and 'done' count. A job that produced nothing should not
    eat somebody's allowance, and a 'skipped' one never ran at all. 'processing'
    is included so that ten recordings uploaded at once cannot each look at an
    empty ledger and all be admitted.

    Fails **open**. If the sum cannot be read, the recording is transcribed:
    refusing to write out somebody's grandmother because a query failed is the
    worse of the two errors, and the cost of being wrong is pennies.
    """
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.transcription_minutes,
                       COALESCE(
                           SUM(
                               -- The provider's figure when it has reported
                               -- one, the client's estimate only until then.
                               COALESCE(t.audio_seconds * 1000, a.duration_ms, 0)
                           ),
                           0
                       ) AS used_ms
                  FROM memoir mo
                  JOIN user_account u ON u.id = mo.created_by_user_id
                  JOIN plan p ON p.code = u.plan_code
                  LEFT JOIN media_asset a
                         ON a.memoir_id = mo.id
                        AND a.kind = 'audio'
                  LEFT JOIN transcript t
                         ON t.asset_id = a.id
                       AND t.status IN ('processing', 'done')
                 WHERE mo.id = %(memoir_id)s
                   AND t.asset_id IS NOT NULL
                 GROUP BY p.transcription_minutes
                """,
                {"memoir_id": memoir_id},
            )
            row = cur.fetchone()

            if row is None:
                # No transcribed audio yet, so nothing is used — but the plan's
                # limit still has to be read, since a limit of zero would mean
                # the very first recording is already over.
                cur.execute(
                    """
                    SELECT p.transcription_minutes
                      FROM memoir mo
                      JOIN user_account u ON u.id = mo.created_by_user_id
                      JOIN plan p ON p.code = u.plan_code
                     WHERE mo.id = %(memoir_id)s
                    """,
                    {"memoir_id": memoir_id},
                )
                limit_row = cur.fetchone()
                if limit_row is None:
                    return False
                row = {
                    "transcription_minutes": limit_row["transcription_minutes"],
                    "used_ms": 0,
                }
    except Exception:
        logger.exception("Could not read the transcription budget for %s", memoir_id)
        return False

    limit_ms = row["transcription_minutes"] * 60_000
    return (row["used_ms"] + incoming_ms) > limit_ms


def request_transcription(asset_id: str) -> None:
    """Send one audio asset for transcription. Never raises.

    Called from a background task after an upload is confirmed, so nothing it
    does can slow down or fail the request that triggered it. That is also why
    every failure in here ends as a logged 'failed' row rather than an
    exception: there is no caller left to catch one, and a memory whose
    transcript failed is still a perfectly good memory.

    Signing and submitting both happen OUTSIDE a database transaction. They are
    network calls to two other services, and holding a Postgres connection open
    across them would tie one up for the duration of somebody else's latency —
    the same reasoning as `begin_upload()`.
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, memoir_id, duration_ms, storage_path,
                   kind::text AS kind, uploaded_at
              FROM media_asset
             WHERE id = %(asset_id)s
            """,
            {"asset_id": asset_id},
        )
        asset = cur.fetchone()

    if asset is None:
        logger.warning("Asked to transcribe asset %s, which does not exist", asset_id)
        return

    if asset["kind"] != "audio":
        return

    if asset["uploaded_at"] is None:
        # A reservation, not a file. Transcribing it would ask AssemblyAI to
        # fetch a URL with nothing behind it.
        logger.info("Asset %s is not uploaded yet; not transcribing", asset_id)
        return

    if not settings.transcription_enabled or not settings.assemblyai_api_key:
        # Not a failure. Nothing went wrong and nothing should be retried, so
        # the row says so plainly rather than pretending an error occurred.
        _record(asset_id, status="skipped")
        return

    if _over_budget(str(asset["memoir_id"]), asset["duration_ms"] or 0):
        # Also not a failure — a deliberate decision not to spend. 'skipped'
        # rather than 'failed' is what stops a retry pass from spending it
        # anyway later.
        logger.info(
            "Memoir %s is at its transcription budget; skipping asset %s",
            asset["memoir_id"],
            asset_id,
        )
        _record(asset_id, status="skipped")
        return

    _record(asset_id, status="queued")

    try:
        audio_url = create_signed_download_url(asset["storage_path"])
    except StorageError as exc:
        logger.error("Could not sign audio for transcription: %s", exc)
        _record(asset_id, status="failed", error=f"could not sign audio: {exc}")
        return

    try:
        job = submit(audio_url)
    except TranscriptionError as exc:
        logger.error("Could not submit asset %s for transcription: %s", asset_id, exc)
        _record(asset_id, status="failed", error=str(exc))
        return

    _record(asset_id, status="processing", provider_id=job["id"])
    logger.info("Asset %s submitted for transcription as job %s", asset_id, job["id"])


def _record(
    asset_id: str,
    *,
    status: str,
    provider_id: str | None = None,
    error: str | None = None,
) -> None:
    """Create or move the transcript row for an asset.

    `ON CONFLICT` rather than a read-then-write: the row may already exist from
    an earlier attempt, and two background tasks racing on the same asset must
    not produce a duplicate-key error in a place with nobody to report it to.
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transcript (asset_id, status, provider_id, error)
            VALUES (%(asset_id)s, %(status)s::transcript_status,
                    %(provider_id)s, %(error)s)
            ON CONFLICT (asset_id) DO UPDATE
               SET status      = EXCLUDED.status,
                   provider_id = COALESCE(EXCLUDED.provider_id,
                                          transcript.provider_id),
                   error       = EXCLUDED.error,
                   requested_at = now()
            """,
            {
                "asset_id": asset_id,
                "status": status,
                "provider_id": provider_id,
                "error": error,
            },
        )


# ---------------------------------------------------------------------------
# Receiving — one write-through, two callers
# ---------------------------------------------------------------------------


def apply_result(provider_id: str, job: dict | None = None) -> bool:
    """Write a finished job into the transcript row. Returns whether it landed.

    The single place a transcript is ever written. Both the webhook and
    `reconcile()` come here, so there is exactly one understanding of what
    AssemblyAI's payload means.

    `job` is optional because the two callers hold different amounts of it.
    The poll has already fetched the whole thing. **The webhook has not**: its
    callback body carries a transcript id and a status and no words at all, so
    passing it straight through would read as a completed job with empty text
    and record a perfectly good recording as having contained no speech. When
    the payload is thin, this fetches the real one.

    Idempotent by design. A webhook and a poll can easily race on the same job
    — the poll goes out, the webhook arrives while it is in flight, both come
    back with the same answer — and writing the same result twice has to be
    harmless. It is: the second write sets identical values, and `_write_terminal`
    refuses to move a row that has already finished.

    Returns False for a `provider_id` this database has never heard of, which
    the webhook route treats as "fine, not ours" rather than as an error.
    """
    # "Completed, but nobody handed me the text" means fetch it. `"text" not in
    # job` rather than a falsy check: a genuinely silent recording comes back
    # with `text` present and empty, and that is a real answer, not a gap.
    if job is None or (job.get("status") == "completed" and "text" not in job):
        try:
            job = fetch(provider_id)
        except TranscriptionError as exc:
            logger.warning("Could not fetch job %s to apply it: %s", provider_id, exc)
            return False

    status = job.get("status")

    if status == "completed":
        text = (job.get("text") or "").strip()
        if not text:
            # A recording of silence, or of a room with nobody speaking in it.
            # Genuinely completed, genuinely empty — and a `done` row with no
            # text would violate the table's own CHECK constraint.
            return _write_terminal(
                provider_id,
                status="failed",
                error="the recording contained no speech",
            )

        segments = _segments_for(provider_id)
        return _write_terminal(
            provider_id,
            status="done",
            text=text,
            segments=segments,
            language_code=job.get("language_code"),
            confidence=job.get("confidence"),
            audio_seconds=_audio_seconds(job),
        )

    if status == "error":
        return _write_terminal(
            provider_id,
            status="failed",
            error=job.get("error") or "the provider did not say why",
        )

    # 'queued' or 'processing' — nothing to write yet, and nothing wrong.
    return False


def _audio_seconds(job: dict) -> int | None:
    """How long the provider says the audio was, in whole seconds.

    Defensive about the field: a provider that stops sending it, renames it, or
    returns something unparseable must not take down a transcript that is
    otherwise perfectly good. None simply means the budget keeps using the
    client's estimate for this one.
    """
    raw = job.get("audio_duration")
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _segments_for(provider_id: str) -> list[dict]:
    """Paragraph segments, reduced to the three fields worth keeping.

    AssemblyAI's paragraph objects carry confidence and a nested word array
    too. Both are dropped here: the word array is the 750 KB-per-hour thing
    this feature deliberately does not store, and a per-paragraph confidence
    has no reader.
    """
    return [
        {
            "start": paragraph.get("start"),
            "end": paragraph.get("end"),
            "text": paragraph.get("text", ""),
        }
        for paragraph in paragraphs(provider_id)
        if paragraph.get("text")
    ]


def _write_terminal(
    provider_id: str,
    *,
    status: str,
    text: str | None = None,
    segments: list[dict] | None = None,
    language_code: str | None = None,
    confidence: float | None = None,
    audio_seconds: int | None = None,
    error: str | None = None,
) -> bool:
    """Move a row to a final state. False if no row holds that provider id.

    The UPDATE is filtered on `status NOT IN (terminal)` so a late webhook
    cannot overwrite a result the poll already recorded, or vice versa. It
    still returns True in that case — the outcome the caller wanted is a fact,
    whoever wrote it.
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE transcript
               SET status        = %(status)s::transcript_status,
                   text          = COALESCE(%(text)s, text),
                   -- Cast explicitly. COALESCE takes its type from the first
                   -- argument, and an untyped or `json` parameter beside a `jsonb`
                   -- column is a CannotCoerce error at runtime, not at import.
                   segments      = COALESCE(%(segments)s::jsonb, segments),
                   language_code = COALESCE(%(language_code)s, language_code),
                   confidence    = COALESCE(%(confidence)s, confidence),
                   audio_seconds = COALESCE(%(audio_seconds)s, audio_seconds),
                   error         = %(error)s,
                   completed_at  = now()
             WHERE provider_id = %(provider_id)s
               AND status NOT IN ('done', 'failed', 'skipped')
         RETURNING asset_id
            """,
            {
                "provider_id": provider_id,
                "status": status,
                "text": text,
                "segments": Jsonb(segments) if segments is not None else None,
                "language_code": language_code,
                "confidence": confidence,
                "audio_seconds": audio_seconds,
                "error": error,
            },
        )
        if cur.fetchone() is not None:
            return True

        # Nothing updated. Either we have never seen this job, or it was
        # already finished — two very different things, and only the first is
        # worth telling the caller about.
        cur.execute(
            "SELECT 1 FROM transcript WHERE provider_id = %(provider_id)s",
            {"provider_id": provider_id},
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def transcripts_for_assets(cur, asset_ids: list[str]) -> dict[str, dict]:
    """Every transcript for a set of assets, keyed by asset id.

    Takes a cursor so it composes inside the caller's transaction — the same
    convention `owned_memoir` and friends follow. One query for any number of
    assets, because `_attach_assets` is already careful not to turn an archive
    of twelve memories into twenty-five round trips and this must not undo
    that.

    `provider_id` and `error` are selected but never leave the API; the
    Pydantic response model drops them. They are here because `reconcile`
    needs the first and the log wants the second.
    """
    if not asset_ids:
        return {}

    cur.execute(
        """
        SELECT asset_id, status::text AS status, provider_id, text, segments,
               language_code, confidence, requested_at
          FROM transcript
         WHERE asset_id = ANY(%(asset_ids)s)
        """,
        {"asset_ids": asset_ids},
    )
    return {str(row["asset_id"]): row for row in cur.fetchall()}


def reconcile(asset_ids: list[str]) -> None:
    """Chase transcripts that are still in flight. Never raises.

    The safety net. Called from a route handler before it reads memories back,
    OUTSIDE any transaction — these are outbound HTTP calls and they must not
    be made while holding a database connection.

    Bounded twice over: only jobs older than a few seconds, and only a handful
    per request. In production the webhook has nearly always arrived first and
    this does nothing at all.
    """
    if not asset_ids:
        return

    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT provider_id
              FROM transcript
             WHERE asset_id = ANY(%(asset_ids)s)
               AND status = 'processing'
               AND provider_id IS NOT NULL
               AND requested_at < now() - make_interval(secs => %(age)s)
             ORDER BY requested_at
             LIMIT %(limit)s
            """,
            {
                "asset_ids": asset_ids,
                "age": _RECONCILE_AFTER_SECONDS,
                "limit": _RECONCILE_LIMIT,
            },
        )
        pending = [row["provider_id"] for row in cur.fetchall()]

    for provider_id in pending:
        try:
            apply_result(provider_id, fetch(provider_id))
        except TranscriptionError as exc:
            # Reading an archive must not fail because a third party is having
            # a bad afternoon. The transcript stays 'processing' and the next
            # read tries again.
            logger.warning("Could not check job %s: %s", provider_id, exc)


def to_payload(row: dict | None) -> dict | None:
    """Shape a transcript row for the API, or None if there is not one.

    `segments` comes back from psycopg as parsed JSON already, but a row
    written by something else could hold a string; parsing defensively costs
    nothing and turns a would-be 500 into a missing paragraph break.
    """
    if row is None:
        return None

    segments = row.get("segments")
    if isinstance(segments, str):
        try:
            segments = json.loads(segments)
        except ValueError:
            segments = None

    return {
        "status": row["status"],
        "text": row.get("text"),
        "segments": segments,
        "language_code": row.get("language_code"),
        "confidence": row.get("confidence"),
    }


def refresh_pending(memories: list[dict]) -> list[dict]:
    """Chase any in-flight transcripts in a list of memories, and patch them in.

    The safety net's entry point, called by the two list routes after the
    memories are built and the transaction is closed. It has to work that way
    round: the asset ids come out of the read, and the HTTP calls must not be
    made while holding a database connection.

    Costs nothing in the ordinary case — no pending transcripts means no
    queries and no outbound calls. In production the webhook has usually
    already arrived and this returns on the first line. On a laptop, where
    there is no webhook at all, this is the whole delivery mechanism.
    """
    pending = [
        str(asset["id"])
        for memory in memories
        for asset in memory.get("assets", [])
        if (asset.get("transcript") or {}).get("status") not in TERMINAL
        and asset.get("transcript") is not None
    ]
    if not pending:
        return memories

    reconcile(pending)

    # Re-read only the transcripts that were in flight, and only if something
    # might have changed. One query, no re-signing of URLs.
    with db() as conn, conn.cursor() as cur:
        fresh = transcripts_for_assets(cur, pending)

    for memory in memories:
        for asset in memory.get("assets", []):
            row = fresh.get(str(asset["id"]))
            if row is not None:
                asset["transcript"] = to_payload(row)

    return memories
