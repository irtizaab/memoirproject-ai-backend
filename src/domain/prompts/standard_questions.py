# The questions this product ships with.
#
# Content, not code. There is no logic in this file worth reading — the value
# is the sentences, and they were written rather than generated.
#
# ---------------------------------------------------------------------------
# What these are for
# ---------------------------------------------------------------------------
# Two jobs, and the second is the important one.
#
# They are what a memoir uses when the owner has not asked for anything else,
# which is most memoirs on their first day. And they are the fallback whenever
# generation fails — no key, no network, the switch off, a refused prompt. A
# contributor must never reach the page and find nothing to answer, because the
# blank page is the entire problem this feature exists to solve.
#
# ---------------------------------------------------------------------------
# How they were written
# ---------------------------------------------------------------------------
# Every one of them obeys the same rules the generated ones do, which is why
# the rules live in a prompt *and* here — the constraints are the product's,
# not the model's:
#
#   - About the subject, never about the contributor's grief. "What did she
#     keep on her desk" is the job. "How did losing her feel" is not, and a
#     memoir is not a condolence book.
#   - Concrete. A question that would fit any memoir gets an answer that fits
#     any memoir.
#   - Answerable in two sentences, by somebody standing up, on a phone, who was
#     sent a link by a relative and has four minutes.
#   - No praise, no encouragement, no exclamation marks, no progress language.
#   - Under twenty words, ending in a question mark.
#
# `{name}` is the subject's first name and is substituted at read time. It is
# deliberately the *first* name: these are read by people who knew them.
#
# ---------------------------------------------------------------------------
# Why the groups differ
# ---------------------------------------------------------------------------
# What you ask a widow is not what you ask a colleague. A spouse was there for
# the ordinary evenings nobody else saw; a grandchild remembers being small
# around somebody already old; a friend knew a version of them the family never
# met. Asking all three the same five questions gets the same five answers
# three times.
#
# The keys are the `relationship_group` enum from migration 0001. Every value
# must appear — a group with no questions is an empty contribute page for
# everybody in it, and `test_standard_questions` fails if one is missing.

# The subject's first name, or a neutral stand-in if there is nothing to slice.
# "them" reads badly in half these sentences, so a memoir with a one-word name
# still gets that word.
_FALLBACK_NAME = "them"


STANDARD_QUESTIONS: dict[str, list[str]] = {
    # Sons and daughters. They remember the house, the rules, and the version
    # of their parent who was still becoming somebody.
    "child": [
        "What did a normal evening at home with {name} sound like?",
        "What did {name} always say, that you can still hear?",
        "What was {name} like when something went wrong?",
        "What did {name} do that you only understood much later?",
        "What did {name} teach you without ever sitting you down?",
    ],
    # Grandchildren met somebody already finished. They remember detail and
    # atmosphere rather than events, so the questions ask for detail.
    "grandchild": [
        "What did {name}'s house smell like, and what was always in it?",
        "What did {name} let you do that nobody else would?",
        "What story did {name} tell more than once?",
        "What did {name} keep that seemed to matter to them?",
        "What did you and {name} do together that was just yours?",
    ],
    # The person who was there for the parts nobody else saw. These are the
    # questions most likely to produce something no one else in the archive can
    # give, which is why they ask about ordinary days rather than about the
    # marriage.
    "spouse_partner": [
        "What was an ordinary Sunday with {name} like?",
        "What did {name} worry about that other people never saw?",
        "What was {name} proudest of, whether or not they said so?",
        "What made {name} laugh, every time?",
        "Where were you both happiest, and what were you doing?",
    ],
    # Friends knew a version the family never met, and often the earliest one.
    # Asking for it directly is the whole point of inviting them.
    "friend": [
        "How did you and {name} meet?",
        "What was {name} like in a room full of people?",
        "What did you and {name} do that the family probably never knew about?",
        "What did {name} do for you that you have not forgotten?",
        "What was {name} like before the rest of us knew them?",
    ],
    # The subject, recording their own life. A different voice entirely: nobody
    # is remembering here, they are telling. Second person would be absurd, so
    # these are the only questions in the file that do not use {name}.
    "self": [
        "Where did you grow up, and what was the house like?",
        "What work did you do, and how did you end up doing it?",
        "Who mattered most to you, and how did you meet them?",
        "What is the day you would live again?",
        "What would you want the people after you to know?",
    ],
    # Everybody else: cousins, colleagues, neighbours, in-laws, and — until the
    # contribute form asks — anyone who has not said which they are. So these
    # have to work for a stranger and for a brother-in-law equally, which means
    # they ask about the subject rather than about the relationship.
    "other": [
        "How did you know {name}?",
        "What is the first thing you think of when you think of {name}?",
        "What was {name} like to spend time with?",
        "What did {name} care about?",
        "What is one day with {name} you still remember clearly?",
    ],
}


def standard_for(relationship: str, subject_name: str | None) -> list[str]:
    """The shipped questions for one group, with the subject's name filled in.

    An unknown group falls back to `other` rather than raising. The caller is
    passing a value out of the database, so an unknown one means the enum grew
    and this file was not updated — which should degrade to the generic set
    rather than take down the contribute page for everybody in the new group.
    """
    questions = STANDARD_QUESTIONS.get(relationship) or STANDARD_QUESTIONS["other"]

    # First name only. These are read by people who knew them, and "What did
    # Eleanor Margaret Marsh always say" is not how any of them think of her.
    first = (subject_name or "").strip().split(" ")[0] or _FALLBACK_NAME

    return [question.format(name=first) for question in questions]
