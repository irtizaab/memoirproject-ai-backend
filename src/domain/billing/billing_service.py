# Domain layer for what an account is entitled to.
#
# There is no payment in here, and that is not an omission — nothing takes
# money yet. What this answers is the question the billing screen asks: which
# plan is this account on, and how much of its storage has been used.
#
# When Stripe arrives it adds a `stripe_client.py` integration and a
# subscription table, and this file grows a "is the subscription actually paid
# up" check. The shape of what it returns does not change.

import logging

from src.domain.memories.memory_service import storage_used_bytes
from src.integrations.db import db

logger = logging.getLogger(__name__)


def get_billing_overview(user_id: str) -> dict | None:
    """The plan this account is on, and its real storage consumption.

    Returns None if the account has no `user_account` row — which happens to
    someone who signed up but never claimed a draft. That is an ordinary
    state, not an error, and the route turns it into a 404 the frontend reads
    as "nothing to bill for yet".

    The `used_bytes` figure is summed from confirmed uploads, never from
    anything the client reported. See `storage_used_bytes`.
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.code,
                   p.name,
                   p.tagline,
                   p.price_cents,
                   p.currency,
                   p.billing_interval,
                   p.storage_limit_bytes
              FROM user_account u
              JOIN plan p ON p.code = u.plan_code
             WHERE u.id = %(user_id)s
            """,
            {"user_id": user_id},
        )
        plan = cur.fetchone()

    if plan is None:
        return None

    return {
        "plan": plan,
        "storage": {
            "used_bytes": storage_used_bytes(user_id),
            "limit_bytes": plan["storage_limit_bytes"],
        },
        # No renewal date, and none invented. Nothing has been charged, so
        # there is nothing to renew, and a plausible-looking date on a billing
        # screen would be a lie about money.
        "renews_on": None,
        "payments_enabled": False,
    }


def list_plans() -> list[dict]:
    """Every plan that can still be signed up for, cheapest first.

    Public — this is a price list, and the pricing screen reads it before the
    billing screen exists for that account. Ordered by price rather than by
    code so the segmented control renders monthly then yearly without the
    frontend deciding on an order of its own.

    Retired plans are excluded. Accounts stay on them (`is_available = false`
    rather than a delete), and `get_billing_overview` will still report one
    correctly — it joins on the code, not on availability.
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT code,
                   name,
                   tagline,
                   price_cents,
                   currency,
                   billing_interval,
                   storage_limit_bytes
              FROM plan
             WHERE is_available
             ORDER BY price_cents
            """
        )
        return cur.fetchall()


def set_plan(user_id: str, code: str) -> dict | None:
    """Move an account onto a plan, and return its refreshed billing overview.

    This is an entitlement change, not a payment. Nothing is charged, so
    `renews_on` stays null and `payments_enabled` stays false — see
    `get_billing_overview`. It exists because the pricing screen lets someone
    choose a term, and showing them a different one on the billing screen
    afterwards would be a small lie about money.

    Returns None if there is no such account or no such available plan. The
    route turns both into a 404: a caller who names a plan that does not exist
    and a caller who has not claimed a draft yet are equally "nothing here",
    and distinguishing them tells a stranger which codes are real.

    The UPDATE is filtered on `is_available` so a retired plan cannot be
    selected. The foreign key alone would allow it.
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE user_account
               SET plan_code = %(code)s
             WHERE id = %(user_id)s
               AND EXISTS (
                   SELECT 1 FROM plan
                    WHERE code = %(code)s AND is_available
               )
         RETURNING id
            """,
            {"user_id": user_id, "code": code},
        )
        if cur.fetchone() is None:
            return None

    # Read back through the same function the GET uses, so the response cannot
    # drift from what the billing screen would fetch a moment later.
    return get_billing_overview(user_id)
