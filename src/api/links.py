# API layer = FastAPI routes. The contributor's way in.
#
# This is the only router in the app with no authentication, and that is the
# product working as designed, not an oversight: "contributors never create
# accounts". The token in the path is the whole credential.

import logging

from fastapi import APIRouter, HTTPException

from src.domain.links.link_service import resolve_link
from src.models.memoir_models import LinkInvitation

logger = logging.getLogger(__name__)

router = APIRouter(tags=["links"])


@router.get("/j/{token}", response_model=LinkInvitation)
def get_invitation(token: str):
    """Resolve a share link into the memoir it points at.

    `/j/` is short on purpose. This URL gets forwarded in WhatsApp messages
    and read aloud over the phone, so every character is one more chance to
    mistype it.

    No `Depends(current_user)` here - adding one would break the core promise
    of the product. What protects this data instead is that the token is 24
    random bytes and is checked against `revoked_at IS NULL`, so a link that
    escapes into the wrong hands can be killed.

    `response_model` is doing real work on this route. It filters the outgoing
    dict down to the fields declared on LinkInvitation, so if someone later
    adds a column to that SELECT, it cannot reach an anonymous caller by
    accident. The owner's private `never_forget` answer is excluded for
    exactly that reason.
    """
    invitation = resolve_link(token)

    # Unknown and revoked look identical from out here, on purpose.
    if invitation is None:
        raise HTTPException(status_code=404, detail="link not found")

    return invitation
