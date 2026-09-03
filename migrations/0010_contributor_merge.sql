BEGIN;

-- ============================================================================
-- 0010 — one person, one entry.
--
-- The problem this solves
-- ----------------------------------------------------------------------------
-- A contributor is recognised by an opaque token kept in their browser, and by
-- nothing else. That is the right identifier — they have no account, often no
-- email, and the whole product depends on them needing neither — but it means
-- the same human arriving on a second device is a second `memoir_participant`
-- row. Their aunt appears twice in the contributors list, with her memories
-- split between the two.
--
-- Why the fix is not "match on the name"
-- ----------------------------------------------------------------------------
-- Because two people genuinely share a name. Two cousins called Ali, or two
-- people who both type "Mum". Merging those two automatically would put one
-- person's memories under the other's name, let one of them read the other's
-- contributions, and there would be no way to tell it had happened. The owner
-- knows their own family; this schema does not. So the merge is an action the
-- owner takes, and the database only records the result.
--
-- Why a column rather than deleting the losing row
-- ----------------------------------------------------------------------------
-- The losing device still holds the losing token. Delete that row and the
-- token resolves to nothing, so the next contribution from that phone creates
-- a *third* participant — reintroducing the exact bug the merge was meant to
-- fix, and doing it silently.
--
-- Keeping the row and pointing it at the winner means both tokens keep working
-- and both lead to one person. `_resolve_contributor` follows the pointer.
-- ============================================================================

ALTER TABLE memoir_participant
    ADD COLUMN merged_into uuid,

    -- Composite, referencing (memoir_id, id) rather than id alone. The same
    -- device the rest of the schema uses: it makes merging someone into a
    -- participant of a *different* memoir impossible at the database level,
    -- not merely unlikely in the application.
    --
    -- SET NULL names its column explicitly, because the default form would try
    -- to null `memoir_id` too and that column is NOT NULL. Postgres 15+.
    ADD CONSTRAINT participant_merge_target
        FOREIGN KEY (memoir_id, merged_into)
        REFERENCES memoir_participant (memoir_id, id)
        ON DELETE SET NULL (merged_into),

    -- A row merged into itself would make `_resolve_contributor` loop.
    ADD CONSTRAINT participant_not_merged_into_self
        CHECK (merged_into IS NULL OR merged_into <> id),

    -- The owner is never a duplicate of anyone. They arrive through signup,
    -- not through the link, and `participant_one_owner_per_memoir` already
    -- guarantees there is exactly one of them.
    ADD CONSTRAINT participant_owner_never_merged
        CHECK (role <> 'owner' OR merged_into IS NULL);

-- Every read of the contributors list filters on this. Partial, because a
-- merged row is only ever reached by following a token — never by listing.
CREATE INDEX participant_live_idx
    ON memoir_participant (memoir_id) WHERE merged_into IS NULL;

-- ----------------------------------------------------------------------------
-- Note on depth
--
-- Merging B into A, then A into C, would leave B pointing at a row that is
-- itself merged. `_resolve_contributor` follows one hop only, deliberately —
-- an unbounded loop over a column any future code could make circular is a
-- hang, and a hang in the one endpoint contributors reach.
--
-- `merge_participants` prevents the case instead: it refuses to merge into a
-- participant that is already merged, and re-points anything already pointing
-- at the loser. So a chain never forms and one hop is always enough.
-- ----------------------------------------------------------------------------

COMMIT;
