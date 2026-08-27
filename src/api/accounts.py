# API layer = FastAPI routes. Everything about the signed-in caller themselves.

import logging

from fastapi import APIRouter, Depends

from src.api.dependencies import CurrentUser, current_user
from src.domain.memoirs.memoir_service import list_memoirs_for_owner
from src.models.memoir_models import AccountOverview

logger = logging.getLogger(__name__)

router = APIRouter(tags=["account"])


@router.get("/me", response_model=AccountOverview)
def get_me(user: CurrentUser = Depends(current_user)):
    """Who am I, and what do I own.

    The frontend calls this on load to decide what to show: no memoirs means
    send them into onboarding, one or more means show the dashboard with the
    share link. It is also the simplest way to check a token is working.

    The identity fields come from the verified token rather than from
    user_account, which is why this returns 200 for someone who has signed up
    but never claimed a draft - they have no user_account row yet, and that is
    a normal state, not a 404. `memoirs` is just empty.

    The ownership filter lives in the query (`WHERE created_by_user_id = ...`),
    not in a check after the fact. That is the rule for every query in this
    codebase: Row Level Security is enabled with zero policies and this API
    connects as a role that bypasses it, so a query without its ownership
    filter is a data leak, not a style problem.
    """
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "memoirs": list_memoirs_for_owner(user.id),
    }
