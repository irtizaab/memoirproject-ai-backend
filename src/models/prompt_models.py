# Pydantic request/response shapes for the question library.
#
# Two audiences, and the gap between them is the reason this file is worth
# reading carefully:
#
#   the owner        sees ids, sources, the mode, and their own notes. They are
#                    editing the library.
#   a contributor    sees question text. Nothing else.
#
# `ContributorQuestions` at the bottom is deliberately the thinnest model in the
# file. A contributor holding a forwarded link has no reason to learn which
# questions were written by a model and which by the family, what the owner
# typed about the subject to produce them, or the ids of rows they cannot edit.
# A field declared there can reach whoever the link was passed to; a field not
# declared there cannot, whatever the SQL selected.

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# The `relationship_group` enum from migration 0001, mirrored. A Literal rather
# than a free string so a bad value is a 422 naming the field instead of a
# less legible 22P02 from Postgres.
RelationshipGroup = Literal[
    "child", "grandchild", "spouse_partner", "friend", "self", "other"
]

# The `prompt_source` enum from 0014. 'owner' is what a question becomes when
# it is edited by hand, which is what stops a reprompt from deleting it.
PromptSource = Literal["ai", "standard", "owner"]

# The `questions_mode` enum. Two real answers, not a switch — see the migration.
QuestionsMode = Literal["standard", "custom"]


# ---------------------------------------------------------------------------
# The owner's side
# ---------------------------------------------------------------------------


class Question(BaseModel):
    """One question in the library, as its owner sees it."""

    id: UUID
    relationship: RelationshipGroup
    ordinal: int
    body: str
    source: PromptSource
    updated_at: datetime


class QuestionGroup(BaseModel):
    """One relationship's questions, in reading order.

    Grouped server-side rather than returned flat, because the screen is
    grouped and doing it in the client means every consumer of this endpoint
    reimplements the same `reduce`.
    """

    relationship: RelationshipGroup
    questions: list[Question] = Field(default_factory=list)


class QuestionLibrary(BaseModel):
    """The whole questions screen in one response.

    One round trip rather than three, for the same reason `MemoirReading` is
    one: the screen renders the mode, the notes and the list together, and
    three requests to build one page is three chances to show half of it.

    `groups` carries every relationship group, including empty ones, so the
    screen can show a group that has no questions yet without inventing the
    list of groups for itself.
    """

    memoir_id: UUID
    subject_name: str
    mode: QuestionsMode

    # What the owner last told us about the subject. Returned so that asking
    # for a fresh set does not mean retyping it. Owner-only, and absent from
    # every contributor-facing model in this file.
    subject_notes: str | None

    groups: list[QuestionGroup] = Field(default_factory=list)

    # The shipped set, filled in with the subject's first name, so the screen
    # can show what 'standard' mode actually means rather than describing it.
    #
    # `StandardGroup`, not `QuestionGroup`, and the difference is the point:
    # these are strings from a constant, not rows. They have no id, no
    # `source` and no `updated_at`, because there is nothing to edit and
    # nothing to have edited it. Reusing the row model here would mean
    # inventing five fields per question to satisfy a schema.
    standard: list["StandardGroup"] = Field(default_factory=list)


class StandardGroup(BaseModel):
    """One group of the shipped questions. Text, and who they are for.

    Read from `domain/prompts/standard_questions.py` every time, never from
    the database — they are the same for every memoir, so there is nothing to
    keep in step.
    """

    relationship: RelationshipGroup
    questions: list[str] = Field(default_factory=list)


class GenerateRequest(BaseModel):
    """Body of POST /memoirs/{id}/questions/generate.

    `notes` is saved before the model is called, so a call that fails does not
    cost the owner the paragraph they typed.

    `replace_edited` is off by default, which is the whole point of tracking
    `source`: asking for a fresh set replaces what the model wrote and leaves
    what the owner wrote alone. Losing a generated question costs a model call.
    Losing a hand-written one costs the sentence a person chose.
    """

    notes: str | None = Field(default=None, max_length=4000)
    replace_edited: bool = False


class QuestionUpdate(BaseModel):
    """Body of PATCH /questions/{id}. One field, and editing flips `source`."""

    body: str = Field(..., min_length=1, max_length=300)


class ModeUpdate(BaseModel):
    """Body of PATCH /memoirs/{id}/questions/mode."""

    mode: QuestionsMode


# ---------------------------------------------------------------------------
# The contributor's side
# ---------------------------------------------------------------------------


class ContributorQuestions(BaseModel):
    """What a contributor is asked. Text, and nothing else.

    No ids: a contributor cannot edit a question, so an id is a handle on
    something they have no route to. No `source`: which of these a model
    drafted is the family's business, and knowing would change how somebody
    answers. No mode, no notes, no counts.

    `relationship` is here because the screen says who these were written for
    — "Questions for a grandchild" — and because it is the value the
    contributor themselves just chose, so it tells them nothing they did not
    supply.
    """

    relationship: RelationshipGroup
    questions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# What the model is asked to return
# ---------------------------------------------------------------------------


class GroupQuestions(BaseModel):
    """One group's questions, as Gemini returns them."""

    relationship: str = Field(
        description=(
            "one of: child, grandchild, spouse_partner, friend, self, other"
        )
    )
    questions: list[str] = Field(
        description="four or five questions, each under twenty words"
    )


class Library(BaseModel):
    """Every group, in one call.

    One request rather than six, because the groups have to differ from each
    other and a model writing them separately has no way to know what it asked
    the others. Six calls also cost six times as much and take six times as
    long, on a screen where somebody pressed a button and is waiting.
    """

    groups: list[GroupQuestions] = Field(default_factory=list)
