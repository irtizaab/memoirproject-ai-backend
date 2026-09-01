# API layer = FastAPI routes. What the account is on, and how full it is.
#
# There is no payment endpoint here yet, on purpose. Checkout, the customer
# portal and the Stripe webhook arrive with the payment pass; these routes exist
# so the billing and pricing screens can show real numbers in the meantime
# rather than each keeping a hardcoded list of its own.

import logging

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import CurrentUser, current_user
from src.domain.billing.billing_service import (
    get_billing_overview,
    list_plans,
    set_plan,
)
from src.models.account_models import BillingOverview, Plan, PlanSelection

logger = logging.getLogger(__name__)

router = APIRouter(tags=["billing"])


@router.get("/plans", response_model=list[Plan])
def get_plans():
    """The price list. Public.

    No credential, because there is nothing here worth protecting and because
    the screen that needs it — onboarding's pricing step — should not depend on
    where it happens to sit relative to signup. It reads the same rows the
    billing screen does, which is the point: two screens quoting one table
    cannot disagree about the price.
    """
    return list_plans()


@router.get("/billing", response_model=BillingOverview)
def get_billing(user: CurrentUser = Depends(current_user)):
    """The caller's plan and their real storage consumption.

    404 for someone who has signed up but never claimed a draft. They have no
    `user_account` row yet, so there is genuinely nothing to bill for — an
    ordinary state on the way through onboarding, not an error worth alarming
    anyone about.
    """
    overview = get_billing_overview(user.id)
    if overview is None:
        raise HTTPException(status_code=404, detail="no account yet")
    return overview


@router.patch("/billing/plan", response_model=BillingOverview)
def patch_billing_plan(
    selection: PlanSelection,
    user: CurrentUser = Depends(current_user),
):
    """Move the account onto a plan term.

    Called when someone picks monthly or yearly on the pricing screen. It
    changes what they are entitled to and what the billing screen quotes back
    at them; it charges nothing, and the response still reports
    `payments_enabled: false` with no renewal date.

    404 covers both "you have no account yet" and "no such plan", deliberately
    undistinguished — see `set_plan`.
    """
    overview = set_plan(user.id, selection.code)
    if overview is None:
        raise HTTPException(status_code=404, detail="no such plan")
    return overview
