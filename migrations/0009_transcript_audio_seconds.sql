-- ============================================================================
-- The Memoir Project — migration 0009
-- How long the provider says the audio actually was.
--
-- ---------------------------------------------------------------------------
-- Why this column exists
--
-- 0008 added a transcription budget, summed from `media_asset.duration_ms`.
-- That figure comes from the browser's MediaRecorder and is sent by the client
-- — which makes the budget a number the client can choose. Understate every
-- duration and the allowance never runs out.
--
-- It is the same mistake `byte_size` was deliberately built to avoid: the
-- storage meter asks storage how big an object is rather than believing the
-- uploader, precisely so the number underneath a limit is not the limited
-- party's to set.
--
-- AssemblyAI reports the true duration of what it processed. That figure
-- arrives with the finished job, so it cannot gate admission — a recording has
-- to be submitted before anyone knows how long it really was. The design is
-- therefore two-layered:
--
--   admission  - the client's duration_ms, an estimate, cheap and immediate
--   the ledger - this column, authoritative, correcting itself as jobs finish
--
-- Someone who lies about a duration gets one recording through and then finds
-- the ledger has caught up with them.
-- ---------------------------------------------------------------------------

BEGIN;


ALTER TABLE transcript
    ADD COLUMN audio_seconds integer
        CONSTRAINT transcript_audio_seconds_positive
            CHECK (audio_seconds IS NULL OR audio_seconds > 0);

-- NULL until a job completes and reports it, and NULL forever for one that
-- failed or was skipped. The budget query falls back to `duration_ms` in that
-- case, which is the right answer: a job still running should count against the
-- allowance at its estimated length, or ten simultaneous uploads would each see
-- an empty ledger and all be admitted.


COMMIT;
