-- ============================================================================
-- The Memoir Project — migration 0011
-- Chapters: the finished book, and the conversation that outlives it.
--
-- Until now the product could collect a memoir but never show one. This adds
-- the read side: chapters made of blocks, the sources each block was drawn
-- from, and the comment layer that stays open after publication.
--
-- Five tables, two enums, one UNIQUE added to an existing table. Nothing here
-- writes chapters — assembly (the Claude call that reads the whole archive) is
-- a later slice. Rows arrive by SQL until it exists, which is deliberate: the
-- read path and the shape it reads are worth settling before the thing that
-- produces them is expensive to change.
--
-- ---------------------------------------------------------------------------
-- The one idea this migration is built on
-- ---------------------------------------------------------------------------
-- A chapter is not written by anybody. It is *assembled and rephrased* from
-- many people's memories, which makes "who actually said this?" a question the
-- reader is entitled to ask about any sentence. The product constraint is
-- explicit — "Never fabricate… Divergent accounts of the same event are both
-- kept and shown side by side" — so attribution is not metadata bolted on
-- afterwards. It is `block_source`, and it is why a paragraph is a row rather
-- than a string in a `text` column somewhere.
--
-- ---------------------------------------------------------------------------
-- Why character offsets are safe here, and only here
-- ---------------------------------------------------------------------------
-- `block_source` and `comment_thread` both point at a *range of characters*
-- inside a block's text. In an editable document that is the hardest problem in
-- the building: the text moves under the anchors and every offset has to be
-- rebased. It is safe here because publication is immutable. Once a memoir is
-- published its text can never change, so an offset written today still points
-- at the same words in forty years. No rebasing, no orphaned comments.
--
-- That is a real dependency, not a happy accident: if anything ever makes a
-- published chapter editable, everything below needs rewriting.
-- ============================================================================

BEGIN;


-- ============================================================================
-- ENUMS
-- ============================================================================

-- What a block *is*. Deliberately small.
--
-- 'paragraph' is prose. 'pull' is a pulled-out line the assembly step wrote to
-- mark something the archive disagreed about ("Two accounts of the same spring
-- differ. Both are kept.") — it is prose too, set differently, and it carries
-- no sources of its own because it is editorial rather than remembered.
-- 'figure' is a photograph.
--
-- No 'heading': a chapter has exactly one title and it lives on `chapter`.
CREATE TYPE block_kind AS ENUM ('paragraph', 'pull', 'figure');

-- Where a photograph sits on the page.
--
-- 'margin' is the default — a small plate beside the paragraph that earns it.
-- 'inset' is the full width of the text column, breaking the prose, for a
-- photograph that *is* the moment rather than an illustration of it.
--
-- Chosen by rule at assembly time, never by the owner: the onboarding flow
-- promises "matching photographs to the stories that mention them" and gives
-- the owner only the chapter titles to edit. A layout control here would be a
-- promise the product does not make.
CREATE TYPE figure_placement AS ENUM ('margin', 'inset');


-- ============================================================================
-- media_asset gets the composite key the rest of this file needs.
--
-- Every table in 0001 and 0003 that other tables point into carries a
-- redundant-looking UNIQUE (memoir_id, id), so that a child row can foreign-key
-- on the *pair* and make a cross-memoir reference impossible at the database
-- level rather than merely unlikely. media_asset was the one table nothing
-- pointed at, so it never got one.
--
-- `chapter_block` points at it now. One line here; a painful migration later.
-- ============================================================================
ALTER TABLE media_asset ADD CONSTRAINT media_asset_memoir_id_id_key
    UNIQUE (memoir_id, id);


-- ============================================================================
-- 8. chapter — one span of a life, with a title somebody chose.
--
--    `ordinal` rather than a sort on years, because chapters are not always
--    chronological and a life does not divide evenly. It is what "Chapter IV"
--    means, and it is what the contents rail orders by.
--
--    `title` is the only thing the owner edits before publishing. It is NOT
--    NULL and not blank: a chapter with no title is a proposal the review
--    screen would have to invent a name for.
-- ============================================================================
CREATE TABLE chapter (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memoir_id     uuid NOT NULL REFERENCES memoir(id) ON DELETE CASCADE,

    ordinal       smallint NOT NULL,
    title         text NOT NULL,

    -- The years this chapter covers, for the eyebrow and the lifespan mark.
    -- Both nullable: front matter and back matter are chapters too in every
    -- respect except that they are not about a stretch of time.
    from_year     smallint,
    through_year  smallint,

    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT chapter_title_not_blank CHECK (btrim(title) <> ''),
    CONSTRAINT chapter_ordinal_positive CHECK (ordinal >= 0),
    CONSTRAINT chapter_years_ordered CHECK (
        from_year IS NULL OR through_year IS NULL OR from_year <= through_year),

    -- Two chapters cannot both be the fourth.
    UNIQUE (memoir_id, ordinal),

    -- So chapter_block and comment_thread can foreign-key on the pair.
    UNIQUE (memoir_id, id)
);

CREATE INDEX chapter_memoir_idx ON chapter (memoir_id, ordinal);


-- ============================================================================
-- 9. chapter_block — one paragraph, one pulled line, or one photograph.
--
--    `ordinal` is reading order within the chapter. A figure has one too: an
--    inset is placed *in the flow*, so where it falls is part of the text.
--
--    `anchor_block_id` is the paragraph a figure belongs beside. A margin plate
--    is positioned against it rather than against the flow, and the caption
--    ("the hand Eleanor spent sixty years imitating") is only true next to the
--    paragraph that earned it. Stored explicitly rather than inferred from
--    "the block before this one", because an inset and its anchor being
--    adjacent is a convention and conventions get broken silently.
-- ============================================================================
CREATE TABLE chapter_block (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memoir_id       uuid NOT NULL REFERENCES memoir(id) ON DELETE CASCADE,
    chapter_id      uuid NOT NULL,

    ordinal         smallint NOT NULL,
    kind            block_kind NOT NULL,

    -- Prose. NULL on a figure.
    text            text,

    -- The photograph. NULL on prose.
    asset_id        uuid,
    placement       figure_placement,
    anchor_block_id uuid,

    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT block_ordinal_positive CHECK (ordinal >= 0),

    -- Prose holds words and nothing else.
    CONSTRAINT block_prose_shape CHECK (
        kind = 'figure' OR (
            btrim(coalesce(text, '')) <> ''
            AND asset_id IS NULL
            AND placement IS NULL
            AND anchor_block_id IS NULL)),

    -- A figure holds a photograph, a placement and the paragraph it belongs to.
    CONSTRAINT block_figure_shape CHECK (
        kind <> 'figure' OR (
            text IS NULL
            AND asset_id IS NOT NULL
            AND placement IS NOT NULL
            AND anchor_block_id IS NOT NULL)),

    UNIQUE (chapter_id, ordinal),

    -- So block_source and comment_thread can foreign-key on the pair.
    UNIQUE (memoir_id, id),

    FOREIGN KEY (memoir_id, chapter_id)
        REFERENCES chapter (memoir_id, id) ON DELETE CASCADE,

    FOREIGN KEY (memoir_id, asset_id)
        REFERENCES media_asset (memoir_id, id) ON DELETE RESTRICT,

    -- A figure cannot be anchored to a paragraph in somebody else's memoir.
    -- Self-referential, so it is declared as a constraint on the pair like the
    -- others rather than inline.
    FOREIGN KEY (memoir_id, anchor_block_id)
        REFERENCES chapter_block (memoir_id, id) ON DELETE CASCADE
);

CREATE INDEX chapter_block_chapter_idx ON chapter_block (chapter_id, ordinal);
CREATE INDEX chapter_block_anchor_idx ON chapter_block (memoir_id, anchor_block_id);

-- ON DELETE RESTRICT above is the interesting one: deleting a photograph that a
-- published chapter is built around would leave a caption describing a gap. The
-- memory delete path has to detach the block first, and until publishing exists
-- there is nothing that can reach this state — so it fails loudly rather than
-- silently taking a figure with it.


-- ============================================================================
-- 10. block_source — which human said this, and which words are theirs.
--
--     The table the "never fabricate" rule lives in. Every clause of assembled
--     prose points back at the memory it came from and the person who left it,
--     so the reader can always ask and always get an answer.
--
--     `start_offset` / `end_offset` are character offsets into the block's own
--     `text`, half-open [start, end). Both NULL means the whole block is from
--     this source — the ordinary case for a paragraph one person supplied.
--
--     `diverges` marks a source that *contradicts* the assembled prose and was
--     kept anyway. It is a boolean rather than a note because the reader
--     renders it as a word ("differs") beside the credit, and because the rule
--     it serves is binary: both accounts are kept, neither is corrected.
-- ============================================================================
CREATE TABLE block_source (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memoir_id      uuid NOT NULL REFERENCES memoir(id) ON DELETE CASCADE,
    block_id       uuid NOT NULL,

    -- What it was drawn from, and who left that. Both, not just the memory:
    -- the participant is what the credit line names, and reaching it through
    -- the memory would break the moment a memory is ever reattributed.
    memory_id      uuid NOT NULL,
    participant_id uuid NOT NULL,

    start_offset   integer,
    end_offset     integer,

    diverges       boolean NOT NULL DEFAULT false,

    created_at     timestamptz NOT NULL DEFAULT now(),

    -- Either the whole block, or a real range inside it. Not one of each.
    CONSTRAINT source_span_is_whole_or_real CHECK (
        (start_offset IS NULL AND end_offset IS NULL)
        OR (start_offset IS NOT NULL AND end_offset IS NOT NULL
            AND start_offset >= 0 AND end_offset > start_offset)),

    FOREIGN KEY (memoir_id, block_id)
        REFERENCES chapter_block (memoir_id, id) ON DELETE CASCADE,
    FOREIGN KEY (memoir_id, memory_id)
        REFERENCES memory (memoir_id, id) ON DELETE CASCADE,
    FOREIGN KEY (memoir_id, participant_id)
        REFERENCES memoir_participant (memoir_id, id) ON DELETE CASCADE
);

CREATE INDEX block_source_block_idx ON block_source (block_id, start_offset);
CREATE INDEX block_source_memory_idx ON block_source (memoir_id, memory_id);


-- ============================================================================
-- 11. comment_thread — a conversation about one passage.
--
--     The only thing in a published memoir that is allowed to grow. "Once
--     published, this memoir cannot be edited… The comment layer stays open.
--     Your family can keep talking about it for as long as they want."
--
--     Anchored the same way a source is — a block, and optionally a range
--     inside it — so the reader positions both with one piece of arithmetic.
--     Null offsets mean the thread is about the whole paragraph.
-- ============================================================================
CREATE TABLE comment_thread (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memoir_id     uuid NOT NULL REFERENCES memoir(id) ON DELETE CASCADE,
    chapter_id    uuid NOT NULL,
    block_id      uuid NOT NULL,

    start_offset  integer,
    end_offset    integer,

    created_at    timestamptz NOT NULL DEFAULT now(),

    -- Set when somebody marks the conversation finished. Nulled again if it is
    -- reopened, which is why it is a timestamp and not a boolean.
    resolved_at   timestamptz,

    CONSTRAINT thread_span_is_whole_or_real CHECK (
        (start_offset IS NULL AND end_offset IS NULL)
        OR (start_offset IS NOT NULL AND end_offset IS NOT NULL
            AND start_offset >= 0 AND end_offset > start_offset)),

    UNIQUE (memoir_id, id),

    FOREIGN KEY (memoir_id, chapter_id)
        REFERENCES chapter (memoir_id, id) ON DELETE CASCADE,
    FOREIGN KEY (memoir_id, block_id)
        REFERENCES chapter_block (memoir_id, id) ON DELETE CASCADE
);

CREATE INDEX comment_thread_chapter_idx ON comment_thread (chapter_id, created_at);


-- ============================================================================
-- 12. comment — one thing one person said about the passage.
--
--     `participant_id`, never `user_id`. The people who comment are the family
--     who contributed the memories, and they arrived by link and have no
--     account — that is the product's first constraint and it applies here
--     exactly as it applies to memories. A returning commenter is recognised by
--     the same `contributor_token` they already hold.
-- ============================================================================
CREATE TABLE comment (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memoir_id      uuid NOT NULL REFERENCES memoir(id) ON DELETE CASCADE,
    thread_id      uuid NOT NULL,
    participant_id uuid NOT NULL,

    body           text NOT NULL,

    created_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT comment_body_not_blank CHECK (btrim(body) <> ''),

    FOREIGN KEY (memoir_id, thread_id)
        REFERENCES comment_thread (memoir_id, id) ON DELETE CASCADE,
    FOREIGN KEY (memoir_id, participant_id)
        REFERENCES memoir_participant (memoir_id, id) ON DELETE CASCADE
);

CREATE INDEX comment_thread_order_idx ON comment (thread_id, created_at);


-- ============================================================================
-- ROW LEVEL SECURITY
--
-- Same posture as every other table: switched on, zero policies, which denies
-- everything to everyone except the service role. The API's route handlers are
-- what authorize access; this is the second wall.
-- ============================================================================

ALTER TABLE chapter        ENABLE ROW LEVEL SECURITY;
ALTER TABLE chapter_block  ENABLE ROW LEVEL SECURITY;
ALTER TABLE block_source   ENABLE ROW LEVEL SECURITY;
ALTER TABLE comment_thread ENABLE ROW LEVEL SECURITY;
ALTER TABLE comment        ENABLE ROW LEVEL SECURITY;


COMMIT;


-- ============================================================================
-- Two things deliberately absent
--
-- 1. No `published` column on `chapter`. A chapter is as published as its
--    memoir and no more; a second flag is a second answer to one question.
--
-- 2. No table for assembly runs. When the Claude call arrives it will want
--    somewhere to record what it read and when — but writing that table now,
--    with no code against it, is the untested assumption migration 0001 warned
--    about in its own header.
-- ============================================================================
