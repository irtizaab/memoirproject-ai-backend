-- ============================================================================
-- The Memoir Project — migration 0008
-- A ceiling on transcription spend.
--
-- ---------------------------------------------------------------------------
-- The problem this bounds
--
-- Every voice note is transcribed automatically, and nothing refused. A memoir
-- is entitled to 10 GiB; 10 GiB of Opus audio is roughly 700 hours, which costs
-- far more to transcribe than a $3/month plan collects. The median memoir —
-- fifty short notes, a couple of hours — costs well under a dollar, so this has
-- never been a daily problem. It is a tail problem, and tails arrive.
--
-- The storage limit does not help: audio is cheap to store and expensive to
-- read. Two different resources needed two different ceilings.
-- ---------------------------------------------------------------------------

BEGIN;


-- ============================================================================
-- plan.transcription_minutes — how much audio a plan will pay to have written
-- out.
--
--    On `plan` rather than on `memoir`, so it moves with what someone pays for
--    rather than being set per row and drifting. Both Keepsake terms get the
--    same figure: the yearly plan is the same product billed differently, and
--    charging the same entitlement twice would be a strange thing to explain.
--
--    600 minutes — ten hours. Comfortably above what a memoir collects in
--    practice, low enough that nobody can run up a bill that dwarfs what they
--    paid. A limit nobody reaches is a limit nobody has to think about, which
--    is the same reasoning behind the 10 GiB.
-- ============================================================================
ALTER TABLE plan
    ADD COLUMN transcription_minutes integer NOT NULL DEFAULT 600
        CONSTRAINT plan_transcription_minutes_positive
            CHECK (transcription_minutes > 0);


-- ============================================================================
-- Nothing is added to `memory` or `media_asset` to track consumption.
--
-- The figure is summed from `media_asset.duration_ms` for assets whose
-- transcript actually reached a billable state, which is the same discipline
-- the storage meter follows: derive the number from what happened rather than
-- keeping a counter that can drift from it. A counter would need to be correct
-- across a failed job, a deleted memory and a webhook that arrived twice.
--
-- 'failed' and 'skipped' are not counted. A job that produced nothing should
-- not consume somebody's allowance.
-- ============================================================================


COMMIT;
