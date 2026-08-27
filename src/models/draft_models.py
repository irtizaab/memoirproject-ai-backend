# Pydantic models = the request/response "shape" for a feature.
# One models file per feature, matching the name used in api/ and domain/.
#
# This is the contract with the outside world. Someone reading this file should
# be able to tell you exactly what a client is allowed to send, without opening
# a single route or SQL query.

from typing import Literal

from pydantic import BaseModel

# The six values the database's `relationship_group` enum accepts, from
# migrations/0001_slice1.sql. Kept as a named constant so the reason for the
# list is obvious and there is one place to change it if the enum ever grows.
#
# This duplicates a rule the database already enforces, on purpose. The
# database constraint is the guarantee — it holds even if some future code
# path forgets to check. This is the error message: it rejects a bad value
# before the query runs and tells the caller which field was wrong and what
# was allowed, which "invalid_value" from the database cannot do.
RelationshipGroup = Literal[
    "child",
    "grandchild",
    "spouse_partner",
    "friend",
    "self",
    "other",
]


class DraftUpdate(BaseModel):
    """Body of PATCH /drafts/{draft_id}.

    Every field is optional, and that is deliberate: this is a *partial* update.
    The onboarding flow saves one answer at a time as the user moves through the
    questions, so a request that sets only `born_year` has to be legal.

    That's also why the route uses `model_dump(exclude_unset=True)` rather than
    plain `model_dump()`. `exclude_unset` gives you only the keys the client
    actually sent. Without it, every unsent field would come back as None and
    the UPDATE would blank out the answers the user already gave — a partial
    update that quietly wipes data is a nasty bug, and one line prevents it.

    """

    subject_name: str | None = None

    # One of six fixed values — the chips on the relationship screen.
    relationship: RelationshipGroup | None = None

    # Free text: the "in your own words" field beside the chips. Deliberately
    # unconstrained, because the whole point of it is that the six categories
    # don't fit everyone.
    relationship_label: str | None = None
    born_year: int | None = None

    # NULL means "Present" on the years wheel - the subject is still living,
    # so there is no end year. Nullable rather than defaulting to false because
    # "we did not ask" and "no, they have died" are different answers, and the
    # onboarding cannot tell them apart until the wheel is touched.
    #
    # Postgres enforces the pairing: the draft_living_has_no_end_year CHECK
    # rejects a row that claims someone is living AND gives a through_year.
    through_year: int | None = None
    subject_is_living: bool | None = None

    never_forget: str | None = None
