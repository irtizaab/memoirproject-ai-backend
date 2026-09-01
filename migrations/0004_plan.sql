-- ============================================================================
-- The Memoir Project — migration 0004
-- Plans: what an account is entitled to.
--
-- Deliberately NOT a billing system. There is no `subscription` table, no
-- Stripe customer id, no payment status, because nothing takes payments yet.
-- What this does support is the billing *screen*: the name of the plan, its
-- price, and the storage limit the meter is drawn against.
--
-- When Stripe arrives it adds a `subscription` table beside this one and a
-- webhook that writes to it. Nothing here needs to change — which is the whole
-- reason for keeping the entitlement (what you get) separate from the
-- subscription (what you pay and whether it went through).
-- ============================================================================

BEGIN;


-- ============================================================================
-- 8. plan — the tiers on offer.
--
--    A table rather than an enum or a dict in Python, because the billing page
--    displays the price and the tagline, and copy that appears on a screen
--    should not require a deploy to change. Keyed on a short text code rather
--    than a uuid: it appears in URLs and in support conversations, and
--    "keepsake" is a thing a person can say out loud.
-- ============================================================================
CREATE TABLE plan (
    code                text PRIMARY KEY,
    name                text NOT NULL,
    tagline             text NOT NULL DEFAULT '',

    -- Cents, not a float. Money in a floating point column is a bug that shows
    -- up as a one-cent discrepancy two years later.
    price_cents         integer NOT NULL,
    currency            text NOT NULL DEFAULT 'USD',

    storage_limit_bytes bigint NOT NULL,

    -- Whether it can still be signed up for. Old plans are retired rather than
    -- deleted, because accounts stay on them.
    is_available        boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT plan_price_not_negative CHECK (price_cents >= 0),
    CONSTRAINT plan_storage_positive CHECK (storage_limit_bytes > 0)
);

-- The only plan there is. 10 GB because a decade of voice notes and scanned
-- photographs for one life fits comfortably inside it, and a limit nobody
-- reaches is a limit nobody has to think about.
INSERT INTO plan (code, name, tagline, price_cents, storage_limit_bytes)
VALUES (
    'keepsake',
    'Keepsake',
    'For one life, told slowly and kept beautifully.',
    800,
    10737418240   -- 10 GiB
);


-- ============================================================================
-- user_account.plan_code
--
--    Defaulted, and NOT NULL, so there is no such thing as an account without
--    an entitlement. Every account is on Keepsake today because nothing takes
--    payment yet; when it does, this column is what a successful checkout
--    writes to.
--
--    ON DELETE RESTRICT: a plan with accounts on it cannot be deleted out from
--    under them. Retire it with is_available = false instead.
-- ============================================================================
ALTER TABLE user_account
    ADD COLUMN plan_code text NOT NULL DEFAULT 'keepsake'
        REFERENCES plan(code) ON DELETE RESTRICT;


-- ============================================================================
-- ROW LEVEL SECURITY
--
-- `plan` gets RLS like every other table, for consistency of posture. It holds
-- nothing private — it is a price list — but a table that is readable by
-- default is a habit worth not forming.
-- ============================================================================
ALTER TABLE plan ENABLE ROW LEVEL SECURITY;


COMMIT;
