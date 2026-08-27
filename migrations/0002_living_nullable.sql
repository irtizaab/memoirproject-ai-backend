-- Correction File

Begin;

Alter TABLE memoir
	Alter COLUMN subject_is_living DROP NOT NULL,
	Alter COLUMN subject_is_living DROP DEFAULT;

ALTER TABLE memoir_draft
    ALTER COLUMN subject_is_living DROP NOT NULL,
    ALTER COLUMN subject_is_living DROP DEFAULT;

COMMIT;