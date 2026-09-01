-- ============================================================================
-- The Memoir Project — migration 0003
-- Memories and media: the slice that makes the invite link do something.
--
-- Until now a share link resolved to a name and a pair of years and stopped
-- there. This adds the thing people were invited to leave behind, and the
-- object-storage bookkeeping that lets them leave it as a voice note or a
-- photograph rather than only as text.
--
-- Two tables, one column added to an existing one, no changes to 0001 or 0002.
-- ============================================================================

BEGIN;


-- ============================================================================
-- ENUMS
-- ============================================================================

-- How a memory was given. Not how it is stored — a 'voice' memory carries a
-- transcript in body_text once transcription exists, and still stays 'voice',
-- because the enum records what the person did, not what we made of it.
CREATE TYPE memory_kind AS ENUM ('text', 'photo', 'voice');

-- What a stored object is. Deliberately coarser than memory_kind: a photo
-- memory holds images, a voice memory holds audio, and a future 'video' kind
-- would need a new value here as well as there.
CREATE TYPE asset_kind AS ENUM ('image', 'audio');


-- ============================================================================
-- memoir_participant.contributor_token
--
-- A contributor has no account and never will, so nothing identifies them
-- across two visits. Without this, someone who adds a memory, closes the tab,
-- and comes back tomorrow arrives as a stranger and appears in the archive
-- twice under the same name.
--
-- Nullable, and NOT defaulted: owners are identified by their user_id and must
-- not be handed a second, weaker credential that reaches the same memoir.
-- The service mints one only when it creates a contributor.
-- ============================================================================
ALTER TABLE memoir_participant
    ADD COLUMN contributor_token text UNIQUE,
    ADD CONSTRAINT participant_owner_has_no_token CHECK (
        role <> 'owner' OR contributor_token IS NULL);


-- ============================================================================
-- 6. memory — one thing one person remembered.
--
--    The FK is composite on purpose. (memoir_id, participant_id) against
--    memoir_participant's UNIQUE (memoir_id, id) makes it structurally
--    impossible to attribute a memory to somebody from a different memoir —
--    not "unlikely if the code is right", impossible. 0001 added that unique
--    index with exactly this table in mind.
-- ============================================================================
CREATE TABLE memory (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memoir_id      uuid NOT NULL REFERENCES memoir(id) ON DELETE CASCADE,
    participant_id uuid NOT NULL,

    kind           memory_kind NOT NULL,

    title          text,        -- NULL: the contributor screen does not ask
    body_text      text,        -- the recollection, or a caption on a photo

    -- A real date, unlike memoir.born_year, because this is one afternoon
    -- rather than a life: "12 August 1988" is the kind of thing people know
    -- about a specific memory and do not know about a lifespan. NULL is
    -- ordinary and means nobody was asked or nobody could say.
    happened_on    date,

    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),

    -- A text memory with no text is not a memory. Photo and voice carry their
    -- content in media_asset, so their body_text may legitimately be empty.
    CONSTRAINT memory_text_has_body CHECK (
        kind <> 'text' OR btrim(coalesce(body_text, '')) <> ''),

    FOREIGN KEY (memoir_id, participant_id)
        REFERENCES memoir_participant (memoir_id, id) ON DELETE CASCADE,

    -- So media_asset can foreign-key on (memoir_id, memory_id) below and get
    -- the same cross-memoir guarantee this table just got.
    UNIQUE (memoir_id, id)
);

CREATE INDEX memory_memoir_idx ON memory (memoir_id, created_at DESC);
CREATE INDEX memory_participant_idx ON memory (memoir_id, participant_id);


-- ============================================================================
-- 7. media_asset — metadata for one object in storage.
--
--    The bytes live in a private Supabase Storage bucket. This table holds a
--    path and never the file: "media never lives in the database as a blob" is
--    a product constraint, and a bytea column here would quietly break it the
--    first time someone found it convenient.
--
--    memory_id is nullable because the upload happens first: the client asks
--    for a signed URL, PUTs the file, confirms it, and only then creates the
--    memory that adopts the asset. An asset that never gets adopted is an
--    abandoned upload — see the note on cleanup at the bottom of this file.
-- ============================================================================
CREATE TABLE media_asset (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memoir_id         uuid NOT NULL REFERENCES memoir(id) ON DELETE CASCADE,
    memory_id         uuid,

    kind              asset_kind NOT NULL,

    -- The object key within the bucket. UNIQUE because two rows pointing at
    -- one object means deleting either would break the other.
    storage_path      text NOT NULL UNIQUE,
    mime_type         text NOT NULL,

    -- Filled in at confirm time from what storage reports, never from what the
    -- client claims — otherwise the storage meter is a number the client gets
    -- to choose. 0 until then.
    byte_size         bigint NOT NULL DEFAULT 0,

    -- Voice only. Milliseconds, because the browser's MediaRecorder reports
    -- them and rounding to seconds at write time loses information you cannot
    -- get back.
    duration_ms       integer,

    original_filename text,

    created_at        timestamptz NOT NULL DEFAULT now(),
    -- NULL until the client confirms the upload finished. A row with a NULL
    -- here is a reservation, not a file.
    uploaded_at       timestamptz,

    CONSTRAINT asset_size_not_negative CHECK (byte_size >= 0),
    CONSTRAINT asset_duration_positive CHECK (
        duration_ms IS NULL OR duration_ms > 0),
    CONSTRAINT asset_duration_is_audio_only CHECK (
        duration_ms IS NULL OR kind = 'audio'),

    FOREIGN KEY (memoir_id, memory_id)
        REFERENCES memory (memoir_id, id) ON DELETE CASCADE
);

CREATE INDEX media_asset_memory_idx ON media_asset (memoir_id, memory_id);

-- The storage meter sums byte_size per owner across every memoir they own, and
-- counts only confirmed uploads. This index is what keeps that a cheap query
-- as an archive grows.
CREATE INDEX media_asset_uploaded_idx
    ON media_asset (memoir_id) WHERE uploaded_at IS NOT NULL;


-- ============================================================================
-- ROW LEVEL SECURITY
--
-- Same posture as every other table: switched on, zero policies, which denies
-- everything to everyone except the service role. The API's route handlers are
-- what authorize access; this is the second wall.
-- ============================================================================

ALTER TABLE memory      ENABLE ROW LEVEL SECURITY;
ALTER TABLE media_asset ENABLE ROW LEVEL SECURITY;


COMMIT;


-- ============================================================================
-- Known gap, deliberately not solved here
--
-- An upload that is reserved and never confirmed leaves a media_asset row with
-- uploaded_at IS NULL and, sometimes, a real object in the bucket that nothing
-- references. Neither counts towards the storage meter, so nobody is charged
-- for them, but they accumulate.
--
-- The fix is a scheduled job that deletes assets older than a day with a NULL
-- uploaded_at, and their objects. It needs somewhere to run scheduled work,
-- which this project does not have yet. Written down rather than half-built.
-- ============================================================================
