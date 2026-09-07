# API layer = FastAPI routes. Keep this thin: read the request, call domain/,
# turn the result into a status code.
#
# Compare this file with src/domain/drafts/draft_service.py. There is no SQL
# here, and there is no HTTPException there. That split is the whole point:
# this file owns everything HTTP, and the service owns everything else.

import logging

from fastapi import APIRouter, Header, HTTPException

from src.domain.drafts.draft_service import create_draft, update_draft
from src.models.draft_models import DraftUpdate

logger = logging.getLogger(__name__)

# The prefix is applied to every route below, so a path of "" here means
# "/drafts" and "/{draft_id}" means "/drafts/{draft_id}". `tags` is purely
# cosmetic — it groups these endpoints together on the /docs page.
router = APIRouter(prefix="/drafts", tags=["drafts"])


@router.post("")
async def post_draft():
    """Create an empty draft and hand back its id and secret token.

    Called the moment onboarding starts, before the user has typed anything.
    The browser keeps the token and sends it back on every later update.
    """
    return await create_draft()


@router.patch("/{draft_id}")
async def patch_draft(
    draft_id: str,
    body: DraftUpdate,
    x_draft_token: str = Header(...),
):
    """Save one or more onboarding answers onto an existing draft.

    `x_draft_token` maps to the `X-Draft-Token` request header. FastAPI does
    that conversion automatically — underscores become dashes. The `...` means
    the header is required; a request without it is rejected as a 422 before
    this function ever runs.

    Handlers here are `async def`, like every handler in this codebase. The
    database driver is asynchronous — psycopg's AsyncConnection, borrowed from
    the pool in integrations/db.py — so the query suspends this request and
    lets the worker serve others while Postgres thinks. A plain `def` handler
    could not await it at all.

    The rule that matters is the other half of that: nothing reachable from
    here may call a *synchronous* driver. One `psycopg.connect()` or
    `httpx.get()` blocks the event loop for its whole duration and freezes
    every other request in the process.
    """
    # exclude_unset=True is load-bearing, not a detail. It narrows the model
    # down to only the fields this request actually sent. Without it, every
    # question the user hasn't answered yet would come through as None and get
    # written over the top of answers they already gave.
    fields = body.model_dump(exclude_unset=True)

    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")

    row = await update_draft(draft_id, x_draft_token, fields)

    # The service returns None for "no such draft", "wrong token" and "already
    # claimed" alike. All three become the same 404 with the same message, so
    # the response never reveals which one it was.
    if row is None:
        raise HTTPException(status_code=404, detail="draft not found")

    return row
