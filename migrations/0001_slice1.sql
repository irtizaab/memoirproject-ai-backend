-- ============================================================================
-- The Memoir Project — migration 0001
-- Slice 1: pledge screen through to the invite link on the dashboard.
--
-- Five tables. Nothing here supports memories, media, chapters, comments or
-- billing yet — those arrive in later migrations, when there is code that
-- uses them. A table with no code against it is an untested assumption.
--
-- Run with:  supabase migration new slice1   (then paste this in)
--            supabase db reset
-- ============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid(), gen_random_bytes()
CREATE EXTENSION IF NOT EXISTS citext;     -- case-insensitive email


-- ============================================================================
-- ENUMS
-- Only the four the onboarding flow actually needs.
-- ============================================================================

CREATE TYPE participant_role   AS ENUM ('owner', 'contributor');
CREATE TYPE memoir_status      AS ENUM ('draft', 'published');
CREATE TYPE link_scope         AS ENUM ('contribute', 'view');
CREATE TYPE relationship_group AS ENUM ('child', 'grandchild', 'spouse_partner',
                                        'friend', 'self', 'other');
-- 'co_owner' and 'reader' are deliberately absent: nothing creates them yet.
-- Adding an enum value later is one line; removing one is a rewrite.


-- ============================================================================
-- 1. user_account — owners only. Contributors never get a row here.
--
--    id IS the Supabase auth.users id. One identity, one primary key, no
--    mapping table and no way for the two to drift apart. The tradeoff:
--    you cannot create a user_account for someone who has not signed up,
--    which matters only when you add co-owner invitations later.
-- ============================================================================
CREATE TABLE user_account (
    id          uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email       citext NOT NULL UNIQUE,
    full_name   text   NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);


-- ============================================================================
-- 2. memoir_draft — the answers given before signup.
--
--    This table exists because memoir.created_by_user_id is NOT NULL, so a
--    pre-auth memoir cannot live in `memoir`. Identified by a secret token
--    the API returns to the browser, NOT by a cookie: the frontend and API
--    are on different origins, and cross-site cookies are a fight you do not
--    need. The browser stores the token and sends it as a header.
--
--    Drafts expire. Someone who abandons onboarding leaves a row behind, and
--    those rows contain a real person's name.
-- ============================================================================
CREATE TABLE memoir_draft (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token              text NOT NULL UNIQUE DEFAULT encode(gen_random_bytes(24), 'hex'),

    subject_name       text,           -- NULL: closed the tab on question 1
    relationship       relationship_group,
    relationship_label text,           -- what they typed in "in your own words"
    born_year          smallint,
    through_year       smallint,       -- NULL + is_living=true means "Present"
    subject_is_living  boolean NOT NULL DEFAULT false,
    never_forget       text,           -- the last question; optional by design

    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    expires_at         timestamptz NOT NULL DEFAULT now() + interval '30 days',
    claimed_at         timestamptz,    -- NULL: not yet turned into a memoir.
                                       -- Set, not deleted, so a double-submit
                                       -- cannot create two memoirs.

    CONSTRAINT draft_years_ordered CHECK (
        born_year IS NULL OR through_year IS NULL OR born_year <= through_year),
    CONSTRAINT draft_living_has_no_end_year CHECK (
        NOT (subject_is_living AND through_year IS NOT NULL))
);

CREATE INDEX memoir_draft_expiry_idx ON memoir_draft (expires_at) WHERE claimed_at IS NULL;


-- ============================================================================
-- 3. memoir — the root entity.
--
--    Years, not dates. The onboarding asks for a year and nothing else, so
--    storing a `date` would mean inventing a month and a day that nobody
--    gave you. When you later want exact dates, add nullable
--    subject_born_on / subject_died_on columns beside these — do not
--    retrofit precision onto a value that never had it.
-- ============================================================================
CREATE TABLE memoir (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    subject_name       text NOT NULL,
    born_year          smallint,
    through_year       smallint,       -- NULL + is_living=true renders "Present"
    subject_is_living  boolean NOT NULL DEFAULT false,
    never_forget       text,

    status             memoir_status NOT NULL DEFAULT 'draft',
    published_at       timestamptz,    -- NULL iff status = 'draft'

    created_by_user_id uuid NOT NULL REFERENCES user_account(id) ON DELETE RESTRICT,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT memoir_name_not_blank CHECK (btrim(subject_name) <> ''),
    CONSTRAINT memoir_published_at_matches_status CHECK (
        (status = 'published') = (published_at IS NOT NULL)),
    CONSTRAINT memoir_years_ordered CHECK (
        born_year IS NULL OR through_year IS NULL OR born_year <= through_year),
    CONSTRAINT memoir_living_has_no_end_year CHECK (
        NOT (subject_is_living AND through_year IS NOT NULL))
);

CREATE INDEX memoir_owner_idx ON memoir (created_by_user_id, created_at DESC);


-- ============================================================================
-- 4. memoir_participant — every human's presence in one memoir.
--
--    Not keyed on email. The whole product bets on people arriving through a
--    shared link with no account and often no email at all, so email cannot
--    be the identifier. A returning contributor is recognised by an opaque
--    id the API hands them.
-- ============================================================================
CREATE TABLE memoir_participant (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memoir_id          uuid NOT NULL REFERENCES memoir(id) ON DELETE CASCADE,
    role               participant_role NOT NULL,

    user_id            uuid REFERENCES user_account(id) ON DELETE SET NULL,
                                    -- NULL: no account. The normal case.
    display_name       text NOT NULL,
    relationship       relationship_group NOT NULL DEFAULT 'other',
    relationship_label text,
    email              citext,      -- NULL: arrived via the link, gave nothing

    invited_at         timestamptz, -- NULL: self-arrived rather than invited
    first_opened_at    timestamptz, -- NULL: has never opened the link
    created_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT participant_name_not_blank CHECK (btrim(display_name) <> ''),
    CONSTRAINT participant_owner_has_account CHECK (
        role <> 'owner' OR user_id IS NOT NULL),

    -- Composite key so later tables (memory, media_asset) can foreign-key on
    -- (memoir_id, participant_id) and make cross-memoir references impossible
    -- at the database level. One line now, a painful migration later.
    UNIQUE (memoir_id, id)
);

CREATE UNIQUE INDEX participant_one_owner_per_memoir
    ON memoir_participant (memoir_id) WHERE role = 'owner';
CREATE INDEX participant_memoir_idx ON memoir_participant (memoir_id, role);


-- ============================================================================
-- 5. memoir_link — the shareable link.
--
--    A table rather than a column because tokens must be revocable. An
--    unlisted link will eventually be forwarded, and the only remedy is to
--    kill that token and issue another while keeping a record of the dead one.
-- ============================================================================
CREATE TABLE memoir_link (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memoir_id   uuid NOT NULL REFERENCES memoir(id) ON DELETE CASCADE,
    scope       link_scope NOT NULL DEFAULT 'contribute',
    token       text NOT NULL UNIQUE DEFAULT encode(gen_random_bytes(24), 'hex'),
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz,   -- NULL: still live
    open_count  integer NOT NULL DEFAULT 0
);

-- At most one live link per scope per memoir: the product promises "one link".
CREATE UNIQUE INDEX memoir_link_one_live_per_scope
    ON memoir_link (memoir_id, scope) WHERE revoked_at IS NULL;


-- ============================================================================
-- ROW LEVEL SECURITY
--
-- The API service connects with the service role key, which BYPASSES every
-- policy below. So these policies are not what protects your data in slice 1
-- — your route handlers are. RLS is switched on anyway as a second wall: if
-- an anon or authenticated key ever leaks into client code, it reads nothing.
--
-- Get this wrong and the failure is silent. Test it (step 3 below).
-- ============================================================================

ALTER TABLE user_account        ENABLE ROW LEVEL SECURITY;
ALTER TABLE memoir_draft        ENABLE ROW LEVEL SECURITY;
ALTER TABLE memoir              ENABLE ROW LEVEL SECURITY;
ALTER TABLE memoir_participant  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memoir_link         ENABLE ROW LEVEL SECURITY;

-- No policies are defined. RLS with zero policies denies everything to
-- everyone except the service role. That is the correct starting point:
-- add a policy when you have a query that needs it and a test that proves it.

COMMIT;