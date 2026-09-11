-- ============================================================================
-- The Memoir Project — migration 0016
-- A third way a photograph can sit on a page: in a carousel, between the text.
--
-- One enum value. `figure_placement` was `margin` (beside the prose, in the
-- outer column) and `inset` (full measure, breaking the column). This adds
-- `carousel`: several photographs in the reading flow itself, at the point the
-- paragraph they belong to ends.
--
-- ---------------------------------------------------------------------------
-- Why a placement and not a new block kind or a table
-- ---------------------------------------------------------------------------
-- Because a carousel is not a new kind of thing in the book — it is several
-- figures shown one at a time instead of side by side, and every figure it
-- holds is already a `chapter_block` row with an `asset_id` and an anchor.
--
-- The grouping is therefore implicit and needs nothing stored: the figures
-- anchored to the same paragraph with `placement = 'carousel'` are one
-- carousel, in `ordinal` order. `block_figure_shape` already makes
-- `anchor_block_id` NOT NULL on a figure, so the thing that defines the group
-- is a column the schema was already enforcing.
--
-- The alternative — a `carousel` block kind with figures pointing at it —
-- would need a new block kind, a nullable second anchor, and a rule about
-- what a carousel with one image means. This needs none of that, and a reader
-- that does not implement carousels renders each figure on its own, which is
-- a worse layout rather than a broken one.
--
-- ---------------------------------------------------------------------------
-- What this does not do
-- ---------------------------------------------------------------------------
-- It does not animate anything, and it cannot: `export_service` prints the
-- same rows onto paper, where a carousel is a run of plates after the
-- paragraph. The auto-advance lives in the web reader, with a pause control,
-- because a picture that moves next to prose competes with the prose.
--
-- Note for whoever adds the next value: `ALTER TYPE ... ADD VALUE` cannot be
-- used in the same transaction that adds it, which is why nothing below
-- writes a row with it.
-- ============================================================================

BEGIN;

ALTER TYPE figure_placement ADD VALUE IF NOT EXISTS 'carousel';

COMMIT;
