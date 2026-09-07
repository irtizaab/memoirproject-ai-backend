-- ============================================================================
-- The Memoir Project — migration 0012
-- The passphrase that stands between a forwarded link and a family's memoir.
--
-- Until now a view link was the entire credential. That was right while the
-- reader was unreachable and wrong the moment the owner is handed a link to
-- send to relatives: a link travels through group chats, screenshots and
-- forwarded emails, and every one of those is a copy of the whole book.
--
-- So publishing now takes two things — the link, and a passphrase the owner
-- chooses and tells people separately. Neither alone opens anything.
-- ============================================================================

BEGIN;


-- ============================================================================
-- memoir.view_passphrase_hash — never the passphrase itself.
--
-- One text column rather than separate hash and salt columns, holding a
-- self-describing string:
--
--     scrypt$16384$8$1$<salt base64>$<derived key base64>
--
-- The parameters travel with the value, so raising the work factor later is a
-- change to the hashing function alone: old rows keep verifying with the
-- numbers they were written with, and each is rewritten at the next successful
-- open. Two columns holding a salt and a digest could not say which algorithm
-- produced them, and that is the migration that gets stuck.
--
-- scrypt comes from Python's hashlib. No passlib, no bcrypt, no argon2: this
-- repository had no hashing dependency before today and does not need one now.
-- ============================================================================
ALTER TABLE memoir ADD COLUMN view_passphrase_hash text;


-- A published memoir is a protected memoir.
--
-- The window this closes is small and real: publish sets `status` and the
-- passphrase in one statement, but a later code path that flipped status on
-- its own would leave a finished book openable by anyone holding the link, and
-- nothing would report it. The database refuses that state instead.
--
-- Drafts are unconstrained. A memoir being written has no view link to protect.
ALTER TABLE memoir ADD CONSTRAINT memoir_published_is_protected CHECK (
    status = 'draft' OR view_passphrase_hash IS NOT NULL);


-- The passphrase is not a secret the database should hand back by accident.
-- There is no index on it, and nothing selects it except `open_for_reading`.
-- Noted here because the next person to write `SELECT *` against memoir will
-- be putting a password hash into a response model, and only `response_model`
-- filtering would stop it reaching a browser.


COMMIT;
