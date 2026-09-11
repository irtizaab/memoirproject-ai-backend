-- ============================================================================
-- The Memoir Project — migration 0015
-- The plan: what the model decided the book is, before the book is written.
--
-- One table. It holds the output of `domain/chapters/planner.py` between the
-- moment the model returns it and the moment `assemble()` writes it into
-- `chapter` rows — which until now was the lifetime of a single local
-- variable inside one request.
--
-- ---------------------------------------------------------------------------
-- Why the plan needs to exist as a row at all
-- ---------------------------------------------------------------------------
-- Because the owner has never seen it. A model was deciding, unsupervised,
-- how a family's memoir divides into chapters and what each one is called,
-- and the first and only time anybody could look at that decision was after
-- it had been written into the book. The same argument migration 0014 makes
-- about the question library, one step later in the product: it is written
-- once, read before it is used, and changed by hand when it is wrong.
--
-- Persisting it also takes the model call out of the assemble request. The
-- planner runs for up to five minutes; `assemble()` now reads a row. A
-- generate that dies at a proxy costs a retry instead of a half-written book.
--
-- ---------------------------------------------------------------------------
-- Why `memoir_plan` and not `plan`
-- ---------------------------------------------------------------------------
-- `plan` is migration 0004: the billing plans, keyed by code, seeded by
-- migration and deliberately excluded from the test truncation. Nothing about
-- this table is related to it. The name follows `memoir_draft` and
-- `memoir_prompt` instead — the other two tables that hold something belonging
-- to one memoir that is not yet part of the finished book.
--
-- ---------------------------------------------------------------------------
-- Why the body is jsonb and not tables
-- ---------------------------------------------------------------------------
-- Because `chapter`, `chapter_block` and `block_source` already *are* the
-- relational form of this document, with the constraints and the foreign keys
-- and the character-offset guards. The plan is the draft that becomes those
-- rows. Modelling it a second time, relationally, would mean maintaining two
-- schemas for one shape and writing the conversion between them — and nothing
-- points *into* a plan, so none of what those tables buy applies here.
--
-- What that costs is honest: this column's shape is enforced in Python, by
-- the same Pydantic models the planner answers in, and not by Postgres. If
-- the plan ever grows a field that needs a constraint or an index, that is
-- the signal to promote it to tables.
--
-- ---------------------------------------------------------------------------
-- Why the memories are not in it
-- ---------------------------------------------------------------------------
-- A chapter in the plan carries `memory_ids`, not memory rows. The archive is
-- the authority on what a memory says and who left it, and a plan that copied
-- that text would be a second copy to keep in step — one that could still be
-- holding the words of a memory the owner has since deleted or rewritten.
--
-- So `assemble()` re-reads the archive and joins it to the plan by id, and a
-- memory that has gone since the plan was made simply drops out of it. This
-- is also what keeps `block_source` insertable: a hallucinated or deleted
-- memory id would fail the foreign key, and it is dropped before it gets
-- there.
-- ============================================================================

BEGIN;

CREATE TABLE memoir_plan (
    -- One plan per memoir, so the primary key is the memoir. Generating again
    -- replaces it: there is no history worth keeping of a draft the owner
    -- asked to have thrown away, and a plan the book was built from is
    -- recoverable from the book.
    memoir_id    uuid PRIMARY KEY REFERENCES memoir(id) ON DELETE CASCADE,

    -- The verified plan, exactly as `planner._clean` left it. Verified is the
    -- operative word: every character offset in here was computed by
    -- `planner.verify` with `str.find` against the block text stored beside
    -- it, never taken from the model.
    body         jsonb NOT NULL,

    -- Which of the two assemblers produced this: the model, or the decade
    -- fallback. Stored rather than inferred because the two are otherwise
    -- indistinguishable once written, and "is the model actually running in
    -- production" is a question the owner and the logs should both be able to
    -- answer without reading chapter titles and guessing.
    organised_by text NOT NULL,

    generated_at timestamptz NOT NULL DEFAULT now(),

    -- NULL until the owner changes something. Read before regenerating, so
    -- somebody who has spent an evening renaming chapters is warned before a
    -- fresh plan discards the lot.
    edited_at    timestamptz,

    -- NULL until `assemble()` has written this plan into chapters. Set, it
    -- means the book downstream was built from this document — and the plan
    -- stops being editable, because `comment_thread` and `block_source`
    -- anchor to character offsets in text that now exists in `chapter_block`.
    assembled_at timestamptz,

    CONSTRAINT memoir_plan_organised_by
        CHECK (organised_by IN ('planner', 'by_date')),

    -- A plan cannot have been assembled before it was made. Catches a clock
    -- or an update that set one without the other.
    CONSTRAINT memoir_plan_assembled_after_generated
        CHECK (assembled_at IS NULL OR assembled_at >= generated_at)
);

COMMENT ON TABLE memoir_plan IS
    'The chapter plan for one memoir, between the model call and the book.';


-- ============================================================================
-- ROW LEVEL SECURITY
--
-- Enabled with zero policies, exactly as on every other table. This API is the
-- authorizer; RLS is the second wall for the day a privileged key leaks into a
-- client. See the note in 0001.
-- ============================================================================
ALTER TABLE memoir_plan ENABLE ROW LEVEL SECURITY;

COMMIT;
