# The shipped questions, checked for the things that would break a page.
#
# This set is the fallback for every failure in the feature — no key, no
# network, the switch off, a refused prompt, a group the owner emptied — so a
# gap in it is not a missing string. It is a contributor opening a link a
# grieving relative sent them and finding nothing to answer, which is the exact
# problem the question library exists to solve.
#
# No network, no key, no database.

import pytest

from src.domain.prompts.prompt_service import GROUPS, MAX_PER_GROUP, MIN_PER_GROUP
from src.domain.prompts.standard_questions import STANDARD_QUESTIONS, standard_for


def test_every_group_has_questions():
    """A group with no questions is an empty page for everybody in it."""
    assert set(STANDARD_QUESTIONS) == set(GROUPS)


@pytest.mark.parametrize("group", sorted(STANDARD_QUESTIONS))
def test_each_group_is_the_right_size(group):
    """Four or five: enough to find one you can answer, few enough to read."""
    assert MIN_PER_GROUP <= len(STANDARD_QUESTIONS[group]) <= MAX_PER_GROUP


@pytest.mark.parametrize("group", sorted(STANDARD_QUESTIONS))
def test_every_question_is_a_question(group):
    """Non-blank, and ending in a question mark.

    `prompt_body_not_blank` in migration 0014 would refuse a blank one if these
    were ever written as rows, and the contribute page prints them either way.
    """
    for body in STANDARD_QUESTIONS[group]:
        assert body.strip()
        assert body.rstrip().endswith("?"), body


@pytest.mark.parametrize("group", sorted(STANDARD_QUESTIONS))
def test_no_question_carries_a_progress_or_praise_word(group):
    """The product forbids gamification, and this file is product copy.

    A cheap check for the words that would smuggle it in — a question that
    thanks somebody or counts what is left is the tone this product does not
    have, and it is much easier to add one here by accident than in code.
    """
    banned = ("thank", "great", "!", "remaining", "complete", "left to", "so far")
    for body in STANDARD_QUESTIONS[group]:
        lowered = body.lower()
        for word in banned:
            assert word not in lowered, f"{group}: {body}"


def test_the_groups_actually_differ():
    """A set that fits a widow and a colleague equally is a failed set.

    The reason the library is per-relationship at all. If two groups ever share
    a question, the split has stopped earning its complexity.
    """
    seen: dict[str, str] = {}
    for group, questions in STANDARD_QUESTIONS.items():
        for body in questions:
            assert body not in seen, f"{group} repeats {seen.get(body)}: {body}"
            seen[body] = group


def test_the_name_is_the_first_name_only():
    """These are read by people who knew them, not by a registrar."""
    questions = standard_for("grandchild", "Eleanor Margaret Marsh")

    assert any("Eleanor" in q for q in questions)
    assert not any("Margaret Marsh" in q for q in questions)
    assert not any("{name}" in q for q in questions)


def test_a_missing_name_still_reads():
    """A memoir can reach here before the subject's name is set."""
    for question in standard_for("child", None):
        assert "{name}" not in question
        assert question.strip()


def test_an_unknown_group_falls_back_rather_than_raising():
    """The enum growing must not take down the contribute page.

    The caller is passing a value out of the database. An unknown one means
    `relationship_group` gained a value and `standard_questions.py` was not
    updated — which should degrade to the generic set, not 500 for everybody in
    the new group.
    """
    assert standard_for("second_cousin_twice_removed", "Eleanor") == standard_for(
        "other", "Eleanor"
    )


def test_the_self_group_does_not_name_the_subject():
    """It is the subject writing about their own life. Naming them is absurd."""
    for question in standard_for("self", "Eleanor"):
        assert "Eleanor" not in question
