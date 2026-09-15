-- ============================================================================
-- The Memoir Project — migration 0017
-- The conversation between the owner and the guide, about the plan.
--
-- One table. The owner writes to the guide — "put the war years in one
-- chapter", "why is this divided by decade" — and the guide answers, and may
-- plan the memoir again with the owner's request restated for the builder.
--
-- ---------------------------------------------------------------------------
-- What this is not
-- ---------------------------------------------------------------------------
-- Not a channel a contributor or a reader can reach: it is addressed by the
-- owner's bearer token and by no link. Not a place the memoir's words pass
-- through: the guide is told counts, chapter titles and the reviewer's
-- findings, and nothing a family wrote. And not a record of the book — a
-- message that replanned the outline says so with one flag, and the outline
-- itself is still `memoir_plan`.
--
-- Messages outlive the plan they were about, on purpose: an owner who asks
-- three times for three different divisions should be able to read what they
-- asked and what they were told. They do not outlive the memoir.
-- ============================================================================

BEGIN;

CREATE TABLE memoir_chat_message (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    memoir_id   uuid NOT NULL REFERENCES memoir(id) ON DELETE CASCADE,

    -- Who wrote it. Two values, and the guide is not a participant: it is
    -- not a person and it never signs anything in the book.
    role        text NOT NULL,

    body        text NOT NULL,

    -- True on a guide message that came with a fresh plan. The frontend reads
    -- it to say so beside the message; nothing else does.
    replanned   boolean NOT NULL DEFAULT false,

    created_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT memoir_chat_message_role
        CHECK (role IN ('owner', 'guide')),

    -- Bounded at both ends. An empty message is a click, not a question; the
    -- ceiling is what one prompt can carry alongside the plan's shape.
    CONSTRAINT memoir_chat_message_body_length
        CHECK (length(body) BETWEEN 1 AND 4000),

    -- Only the guide replans.
    CONSTRAINT memoir_chat_message_owner_never_replans
        CHECK (NOT replanned OR role = 'guide')
);

CREATE INDEX memoir_chat_message_by_memoir
    ON memoir_chat_message (memoir_id, created_at);

COMMENT ON TABLE memoir_chat_message IS
    'The owner''s conversation with the guide about how the memoir is planned.';

-- ============================================================================
-- ROW LEVEL SECURITY — enabled with zero policies, as on every other table.
-- ============================================================================
ALTER TABLE memoir_chat_message ENABLE ROW LEVEL SECURITY;

COMMIT;
