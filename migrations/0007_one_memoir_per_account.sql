-- ============================================================================
-- The Memoir Project — migration 0007
-- One account, one memoir. Enforced by the database, not by hope.
--
-- ---------------------------------------------------------------------------
-- The bug this closes
--
-- `useActiveMemoir` on the frontend takes `memoirs[0]` — the newest — because
-- the product has always assumed an account owns exactly one. Nothing enforced
-- it. Running onboarding a second time claimed a second memoir, which then
-- became the "active" one, and every memory in the first became invisible: not
-- deleted, not recoverable through the UI, simply never rendered again.
--
-- One test account had accumulated five memoirs and could see one of them.
-- That is a data-loss bug wearing the costume of a display bug.
--
-- The frontend guard added earlier (redirect away from onboarding when a
-- memoir exists) helps, but a guard in a browser is a convenience. This is the
-- guarantee.
-- ---------------------------------------------------------------------------

BEGIN;


-- ============================================================================
-- Clean slate.
--
-- Requested explicitly: every memoir, memory, asset and transcript across every
-- account is removed so the constraint below can be added to a database with no
-- duplicates to reconcile. The storage objects were deleted separately, before
-- this ran — the rows hold only paths, and dropping them first would have
-- stranded the files with nothing left pointing at them.
--
-- Order matters less than it looks: every child here is ON DELETE CASCADE from
-- `memoir`, so deleting memoirs alone would take memories, participants, links,
-- assets and transcripts with it. The explicit deletes are here so that the
-- intent is visible in the migration rather than implied by five foreign keys
-- in another file.
-- ============================================================================
DELETE FROM transcript;
DELETE FROM media_asset;
DELETE FROM memory;
DELETE FROM memoir_link;
DELETE FROM memoir_participant;
DELETE FROM memoir;

-- Drafts are anonymous and disposable by design. Unclaimed ones now point at
-- nothing meaningful, so they go too.
DELETE FROM memoir_draft;


-- ============================================================================
-- The constraint.
--
-- A plain UNIQUE index rather than a partial one: there is no soft delete on
-- `memoir`, so there is no state in which two rows for one account are
-- legitimate. If a second memoir per account ever becomes a product decision —
-- a family recording a second relative — this is one line to drop, and the
-- frontend gains a switcher. Until then, "the newest one wins and the rest
-- vanish" is not a behaviour anyone chose.
--
-- 23505 already maps to 409 in src/core/error_handlers.py, so a second claim
-- fails honestly even if the service layer forgets to look.
-- ============================================================================
CREATE UNIQUE INDEX memoir_one_per_account
    ON memoir (created_by_user_id);


COMMIT;
