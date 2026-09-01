-- ============================================================================
-- The Memoir Project — migration 0005
-- Plan terms: the same plan, billed two ways.
--
-- 0004 seeded a single Keepsake tier at $8/month. That was never the price the
-- product quotes — onboarding's pricing screen has always said $3/month or
-- $30/year, and it was quoting a hardcoded array rather than this table. One of
-- the two had to be wrong; the screen was right.
--
-- The fix is not just a cheaper number. It is a second row, so the yearly term
-- the pricing screen offers actually exists somewhere an account can be put
-- on, and so both screens can read their prices from here instead of each
-- keeping its own list to drift out of step with.
--
-- Monthly and yearly are the SAME entitlement — same name, same 10 GiB. They
-- differ only in interval and price. Two rows rather than a `price_cents_yearly`
-- column because `user_account.plan_code` has to point at exactly one of them,
-- and a column cannot be pointed at.
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- plan.billing_interval — what the price is per.
--
-- Text with a CHECK rather than an enum: two values that will not grow (a
-- weekly memoir subscription is not a product), and a CHECK is the cheaper of
-- the two to widen if that turns out to be wrong.
--
-- Defaulted to 'month' so the existing row does not need a value supplied, and
-- NOT NULL because a price with no interval is not a price.
-- ----------------------------------------------------------------------------
ALTER TABLE plan
    ADD COLUMN billing_interval text NOT NULL DEFAULT 'month'
        CONSTRAINT plan_interval_known
            CHECK (billing_interval IN ('month', 'year'));


-- ----------------------------------------------------------------------------
-- The correction. 800 -> 300.
--
-- Safe to run against live accounts: `user_account.plan_code` references the
-- code, not the price, so every account on 'keepsake' simply becomes cheaper.
-- Nothing has been charged against the old figure — `payments_enabled` is still
-- false — so there is no billing history this contradicts.
-- ----------------------------------------------------------------------------
UPDATE plan SET price_cents = 300 WHERE code = 'keepsake';


-- ----------------------------------------------------------------------------
-- The yearly term. $30 for twelve months of a $3 plan — two months free, which
-- is the note the pricing screen shows and is worth keeping arithmetically
-- true if either number is ever changed.
-- ----------------------------------------------------------------------------
INSERT INTO plan (
    code, name, tagline, price_cents, storage_limit_bytes, billing_interval
)
VALUES (
    'keepsake_yearly',
    'Keepsake',
    'For one life, told slowly and kept beautifully.',
    3000,
    10737418240,   -- 10 GiB, identical to the monthly term
    'year'
);


-- `keepsake` remains the `user_account.plan_code` default. A new account is on
-- the monthly term until something says otherwise, and nothing about existing
-- accounts changes.


COMMIT;
