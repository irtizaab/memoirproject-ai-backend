-- ============================================================================
-- The Memoir Project — migration 0014
-- The question library: what the family is asked, decided by the owner.
--
-- One table and two columns on `memoir`. The owner writes a line about the
-- subject, a model drafts four or five plain questions for each kind of
-- person who might contribute, and the owner edits them by hand, asks for a
-- fresh set, or switches the whole memoir over to the standard questions that
-- ship with the product.
--
-- This is the promise `features/onboarding/data.ts` has been making since the
-- first screen — "a question library: written prompts for each person, so
-- nobody faces a blank page" — and until now nothing implemented it.
--
-- ---------------------------------------------------------------------------
-- Why the questions belong to the memoir and not to a contributor
-- ---------------------------------------------------------------------------
-- The obvious alternative is to write a question for each person as they
-- arrive, out of what they have just said. That version was built and thrown
-- away, and the reason is worth recording: the owner never saw it. A model was
-- deciding, unsupervised and one person at a time, what a grieving family
-- would be asked about someone they lost — with no way for the person who
-- started the memoir to read those questions, fix one, or refuse them.
--
-- A library is the same feature with the owner put back in the middle of it.
-- It is written once, read before it is used, and changed by hand when it is
-- wrong.
--
-- ---------------------------------------------------------------------------
-- Why questions are grouped by relationship
-- ---------------------------------------------------------------------------
-- What you ask a widow is not what you ask a colleague. `relationship_group`
-- already exists from migration 0001 and already sits on `memoir_participant`,
-- so this reuses that vocabulary rather than inventing a second one.
--
-- Note the dependency this creates: until now every contributor was written to
-- the database as 'other', hardcoded in `resolve_participant`. The contribute
-- form has to ask. Without that question every contributor lands in one group
-- and this column does nothing.
--
-- ---------------------------------------------------------------------------
-- What this table is not
-- ---------------------------------------------------------------------------
-- Not a checklist. No `required`, no `answered`, no count of what is left, and
-- nothing anywhere records which memory came from which question. The product
-- forbids progress indicators, and "3 of 5 answered" is one. A contributor
-- reads the questions and writes what they want to write.
-- ============================================================================

BEGIN;


-- Where a question came from. The vocabulary is small and closed, which is
-- what an enum is for.
--
-- 'owner' is the load-bearing value. A question becomes 'owner' the moment it
-- is edited by hand, and that is what lets "write me a fresh set" replace the
-- model's drafts without silently deleting the ones somebody rewrote
-- themselves. Losing a generated question costs a model call; losing a
-- hand-written one costs the sentence a person chose.
CREATE TYPE prompt_source AS ENUM ('ai', 'standard', 'owner');

-- Which set of questions this memoir actually uses.
--
-- Two values rather than a boolean, because `use_ai_questions = false` reads
-- as a feature being off. Neither of these is off: both are a real answer to
-- "what should my family be asked", and the screen presents them as a choice
-- between two things rather than as a switch.
CREATE TYPE questions_mode AS ENUM ('standard', 'custom');


CREATE TABLE memoir_prompt (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memoir_id     uuid NOT NULL REFERENCES memoir(id) ON DELETE CASCADE,

    -- Who this question is for. Reuses the enum from 0001 rather than a second
    -- vocabulary: 'grandchild' in a question and 'grandchild' on a participant
    -- have to mean one thing, or the join that picks somebody's questions is
    -- quietly wrong.
    relationship  relationship_group NOT NULL,

    -- Reading order within the group. smallint because five is a lot of
    -- questions and fifty would be a different product.
    ordinal       smallint NOT NULL,

    body          text NOT NULL,
    source        prompt_source NOT NULL DEFAULT 'ai',

    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT prompt_body_not_blank CHECK (btrim(body) <> ''),
    CONSTRAINT prompt_ordinal_non_negative CHECK (ordinal >= 0),

    -- One question per position per group. The same rule `chapter` keeps on
    -- (memoir_id, ordinal): two rows claiming position 2 is a list with no
    -- order, and the bug shows up as questions that shuffle between reloads.
    UNIQUE (memoir_id, relationship, ordinal),

    -- Kept for the same reason every other table here keeps one: so a later
    -- table can foreign-key on (memoir_id, id) without making cross-memoir
    -- references possible.
    UNIQUE (memoir_id, id)
);

-- The one query the contribute page runs: this memoir, this group, in order.
CREATE INDEX prompt_group_idx
    ON memoir_prompt (memoir_id, relationship, ordinal);


-- ============================================================================
-- Two columns on `memoir`
-- ============================================================================

-- Which set of questions contributors see.
--
-- Defaults to 'standard' so that a memoir whose owner never opens the
-- questions screen still shows contributors something. The alternative default
-- is an empty page for the family of anyone who did not know the screen
-- existed.
ALTER TABLE memoir
    ADD COLUMN questions_mode questions_mode NOT NULL DEFAULT 'standard';

-- What the owner told us about the subject, in their own words, so the model
-- has something concrete to write questions from.
--
-- Stored rather than passed straight through, for one reason: asking for a
-- fresh set of questions must not mean retyping the paragraph that produced
-- the last set.
--
-- Note this is NOT `never_forget`. That one is the owner's private answer from
-- onboarding and is excluded from every response model that a link can reach
-- (see `LinkInvitation`, `MemoirReading`). This is material deliberately
-- written to be turned into questions other people will read. Two different
-- things, kept in two columns.
ALTER TABLE memoir
    ADD COLUMN subject_notes text;


-- ============================================================================
-- ROW LEVEL SECURITY
--
-- Enabled with zero policies, exactly as on every other table. This API is the
-- authorizer; RLS is the second wall for the day a privileged key leaks into a
-- client. See the note in 0001.
-- ============================================================================
ALTER TABLE memoir_prompt ENABLE ROW LEVEL SECURITY;


COMMIT;


-- ============================================================================
-- Deliberately absent
--
-- 1. No table of standard questions. They are a constant in
--    `domain/prompts/standard_questions.py`, not rows, because they are the
--    same for every memoir and copying forty rows into every new one would
--    make them editable per memoir — which is what 'custom' mode is for.
--
-- 2. No `answered_by` anywhere. See the header: nothing records which memory
--    came from which question, because the only thing that could be built on
--    top of it is a completion meter.
--
-- 3. No cost or token ledger. Generation is five short lists, run by hand, by
--    the one person paying for the memoir. If that changes,
--    `plan.transcription_minutes` in 0008 is the pattern to copy — summed from
--    evidence, never counted into a column.
-- ============================================================================
