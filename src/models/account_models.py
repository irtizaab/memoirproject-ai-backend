# Pydantic models for the contributors and billing screens.
#
# Separate from memoir_models.py because these describe the owner's
# administrative view of a memoir — who is in it, what it costs — rather than
# the memoir itself.

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

# The storage shape is defined once, in memory_models, because the media
# endpoints return it too.
from src.models.memory_models import StorageUsage


class ShareLink(BaseModel):
    """The live contribute link for a memoir.

    `token` and not a URL: the API has no business knowing what domain the
    frontend is served from, and a staging deployment that handed out
    production links would be a real bug. The frontend composes the address.
    """

    token: str
    # A plain fact about how many times the link was opened. Not a score, not a
    # target, and never rendered next to a goal — the product forbids that, and
    # the contributors screen is where the temptation lives.
    open_count: int
    created_at: datetime


class Contributor(BaseModel):
    """One person in a memoir, as the owner sees them.

    Note what is missing: `contributor_token`. It is that person's credential
    for adding memories, and there is no legitimate reason for an owner to hold
    it. `response_model` filtering is what keeps it out even if a future query
    selects it by accident.
    """

    id: UUID
    display_name: str
    role: str
    relationship: str
    relationship_label: str | None

    # NULL means they have never opened the link. Distinct from having opened
    # it and written nothing, which is `memory_count == 0` with a timestamp
    # here — a different situation calling for a different thing to say.
    first_opened_at: datetime | None
    memory_count: int
    last_contribution_at: datetime | None


class ContributorsOverview(BaseModel):
    """Everything the contributors screen needs, in one response."""

    # None when every link has been revoked and none reissued. A real state
    # with a real thing to say about it, not an error.
    link: ShareLink | None
    participants: list[Contributor]


class Plan(BaseModel):
    """A tier, as shown on the billing screen."""

    code: str
    name: str
    tagline: str
    # Cents. The frontend formats it; sending "$3.00" would bake a currency
    # symbol and a locale into an API response.
    price_cents: int
    currency: str

    # What the price is per: "month" or "year". Monthly and yearly Keepsake are
    # two rows with the same name and the same entitlement, so this is the only
    # field that tells them apart on screen.
    billing_interval: str

    storage_limit_bytes: int


class PlanSelection(BaseModel):
    """Which term the account is on.

    Not a payment. Nothing is charged when this is set — it records the choice
    made on the pricing screen so the billing screen agrees with it. When
    Stripe lands, a successful checkout writes the same column and this becomes
    the fallback for the free path, or goes away.
    """

    code: str


class BillingOverview(BaseModel):
    """What the account is on, and how full it is."""

    plan: Plan
    storage: StorageUsage

    # Null until something has actually been charged. Showing a made-up
    # renewal date would be a lie about money, which is the worst kind of
    # placeholder to ship.
    renews_on: datetime | None

    # False until Stripe is wired up. The frontend uses it to disable "Manage
    # plan" and say why, rather than offering a button that goes nowhere.
    payments_enabled: bool
