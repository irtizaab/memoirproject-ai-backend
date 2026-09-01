-- ============================================================================
-- The Memoir Project — migration 0006
-- Transcripts: the words in a recording.
--
-- A voice memory has always stored audio and nothing else. That makes a memoir
-- unsearchable, unreadable on a train, and useless as input to the chapter
-- assembly step that comes later. This is the table that fixes it.
--
-- ---------------------------------------------------------------------------
-- Why the text lives in Postgres and not in the bucket
--
-- An hour of speech is about 9,000 words — roughly 50 KB of UTF-8, or 20 KB
-- once TOAST has compressed it out of the main heap row. The audio for that
-- same hour is about 14 MB. The transcript is well under half a percent of the
-- recording it describes, and it is the half that wants to be queried.
--
-- Putting it in storage instead would buy an HTTP round trip per memory just
-- to display it, no search across a memoir, no excerpt in the archive grid,
-- and a second object to keep in sync every time a memory is deleted.
--
-- The one genuinely large thing AssemblyAI returns is the word-level array —
-- start, end and confidence per word, about 750 KB for that same hour, fifteen
-- times the transcript. We do not ask for it and do not store it. Paragraph
-- segments give the same "jump to this point" ability for a fifth of the size.
-- ---------------------------------------------------------------------------

BEGIN;


-- ============================================================================
-- transcript_status — where a job has got to.
--
--    'queued'     a row exists, nothing has been submitted yet
--    'processing' submitted; the provider is working, provider_id is set
--    'done'       text is present
--    'failed'     the provider gave up; `error` says why, for us not for them
--    'skipped'    deliberately not transcribed (transcription switched off,
--                 or a future per-memoir budget refusing it). Distinct from
--                 'failed' because nothing went wrong and nothing should be
--                 retried.
-- ============================================================================
CREATE TYPE transcript_status AS ENUM (
    'queued', 'processing', 'done', 'failed', 'skipped'
);


-- ============================================================================
-- 9. transcript — one per audio asset.
--
--    A table rather than seven more columns on media_asset. Those columns
--    would be NULL for every photograph ever uploaded, and they carry a
--    lifecycle — queued, processing, done — that has nothing to do with the
--    stored object itself.
--
--    NOT memory.body_text either. That column holds what a *person wrote*, and
--    the contributor form asks for it even on a voice note ("Anything to add
--    in writing?"). A transcript written there would destroy what they typed.
-- ============================================================================
CREATE TABLE transcript (
    -- The primary key *is* the asset. One transcript per recording, enforced
    -- by the shape of the table rather than by a unique index bolted on after,
    -- and ON DELETE CASCADE for free: deleting a memory already takes its
    -- assets, and now takes their transcripts with them.
    asset_id      uuid PRIMARY KEY REFERENCES media_asset(id) ON DELETE CASCADE,

    status        transcript_status NOT NULL DEFAULT 'queued',

    -- Which service produced it. A column and not a constant because a
    -- transcript made by a different provider — or by a person, later — is
    -- still a transcript, and the row should say which.
    provider      text NOT NULL DEFAULT 'assemblyai',

    -- The provider's own id for the job. UNIQUE because the webhook arrives
    -- carrying nothing else: it is the only handle we have for finding this
    -- row again, and a duplicate would make that lookup ambiguous.
    provider_id   text UNIQUE,

    -- The transcript. Unbounded `text`; see the note at the top of this file
    -- for why an hour of it is not a problem.
    text          text,

    -- Paragraph-level segments: [{"start": ms, "end": ms, "text": "..."}].
    -- Enough to render readable blocks now and to jump to a point in the audio
    -- later. Deliberately NOT word-level.
    segments      jsonb,

    -- What the provider detected. The subjects of these memoirs do not all
    -- speak English, and a hardcoded language would return confident nonsense,
    -- so this is asked for rather than assumed.
    language_code text,
    confidence    real,

    -- Why it failed, for the log and for us. Never returned by the API: a
    -- family does not need a provider's error string about their
    -- grandmother's accent, and the audio is unaffected either way.
    error         text,

    requested_at  timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz,

    -- A 'done' transcript with no text is not done. Catches a write-through
    -- that recorded success while dropping the payload.
    CONSTRAINT transcript_done_has_text CHECK (
        status <> 'done' OR text IS NOT NULL),

    CONSTRAINT transcript_confidence_range CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);


-- The reconcile pass asks one question — "what is still in flight?" — and this
-- partial index is the whole answer set. Partial because finished transcripts
-- are the overwhelming majority and none of them belong in it.
CREATE INDEX transcript_pending ON transcript (requested_at)
    WHERE status IN ('queued', 'processing');


-- ============================================================================
-- ROW LEVEL SECURITY
--
-- Enabled with zero policies, as on every other table here. The route handlers
-- are the only protection, and they reach a transcript exclusively through the
-- memory that owns the asset — so a transcript is exactly as visible as the
-- recording it came from, and no more.
-- ============================================================================
ALTER TABLE transcript ENABLE ROW LEVEL SECURITY;


COMMIT;
