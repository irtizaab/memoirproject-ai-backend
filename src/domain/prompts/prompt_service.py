# Domain layer for the question library — what the family is asked, and who
# decides it.
#
# Nothing here imports fastapi. It returns plain dicts and raises its own
# exceptions; api/prompts.py decides which status code each one is.
#
# ---------------------------------------------------------------------------
# The owner is in the middle of this on purpose
# ---------------------------------------------------------------------------
# An earlier version of this file wrote one question per contributor, in the
# moment, out of the memory they had just left. It worked, and it was wrong:
# the owner never saw the questions. A model was deciding unsupervised what a
# grieving family would be asked about somebody they had lost, and the person
# who started the memoir had no way to read those questions, fix one, or say
# no.
#
# So: the model drafts, the owner reads and edits, and only then does anybody
# get asked anything. Everything below is in service of that order.
#
# ---------------------------------------------------------------------------
# Two sets, and neither of them is "off"
# ---------------------------------------------------------------------------
# `questions_mode` is 'standard' or 'custom'. Standard is the set written into
# `standard_questions.py`; custom is what the model drafted and the owner
# edited. A memoir whose owner never opens the questions screen uses standard,
# which is why the default matters: the alternative is a blank contribute page
# for the family of anyone who did not know the screen existed.

import logging

from src.domain.memoirs.access import contributable_memoir, owned_memoir
from src.domain.prompts.standard_questions import STANDARD_QUESTIONS, standard_for
from src.integrations import gemini
from src.integrations.db import db
from src.models.prompt_models import Library

logger = logging.getLogger(__name__)

# The groups a library covers, in the order the screen shows them. Mirrors the
# `relationship_group` enum from migration 0001, narrowest relationship first.
GROUPS = ["child", "grandchild", "spouse_partner", "friend", "self", "other"]

# How many questions per group. Four or five: enough that somebody who cannot
# answer the first can answer the third, few enough that the page is not a
# form. A contributor arrived from a text message and has four minutes.
MIN_PER_GROUP = 4
MAX_PER_GROUP = 5

# How much of the owner's note to send. Long enough for several paragraphs
# about a life, short enough that it cannot become the whole prompt.
_MAX_NOTES_CHARS = 4000


# ---------------------------------------------------------------------------
# The rules the model works under
# ---------------------------------------------------------------------------
#
# The same rules `standard_questions.py` was written to, and they are stated
# twice on purpose: they are the product's constraints, not the model's, and
# the shipped set has to obey them as strictly as the drafted one.
_SYSTEM = """\
A family is recording a memoir about one person. You are writing the questions \
their relatives and friends will be asked, so that nobody opens the page and \
faces an empty box.

You write one set of questions for each group of people, because what you ask \
a widow is not what you ask a colleague. The groups are:

- child: their sons and daughters
- grandchild: their grandchildren
- spouse_partner: their husband, wife, or partner
- friend: their friends
- self: the subject writing about their own life, addressed as "you"
- other: cousins, colleagues, neighbours, and anyone who has not said

Return four or five questions for every one of those six groups. The groups \
must genuinely differ — a set that would work equally well for a widow and a \
colleague is a failed set.

Every question, without exception:

- Asks about the subject, never about the contributor's grief or feelings. \
"What did she keep on her desk?" is the job. "How did losing her feel?" is not.
- Is concrete. A question that would fit any memoir gets an answer that fits \
any memoir. Anchor it in a detail you were actually given.
- Can be answered in two sentences by somebody standing up, on a phone, who \
was sent a link by a relative.
- States nothing you were not told. No dates, places, jobs, illnesses, or \
relationships you were not given. If a fact is needed to ask the question, ask \
for the fact instead.
- Is under twenty words and ends in a question mark.
- Contains no praise, no thanks, no encouragement, no exclamation marks, and \
nothing about progress, counts, or what would "complete" anything.
- Uses the subject's first name, except in the 'self' group, which addresses \
the subject directly as "you" and does not name them.

Do not number the questions. Do not repeat a question across groups."""


class LinkNotUsable(Exception):
    """The share link is unknown, revoked, view-only, or its memoir is sealed.

    One exception for all four, because the API answers one 404 for all four —
    a person holding a dead link cannot act on the difference, and spelling it
    out tells whoever it was forwarded to why it died.
    """


class NothingGenerated(Exception):
    """The model could not draft a library.

    Switched off, no key, unreachable, refused, or a reply with nothing usable
    in it. Raised rather than silently falling back, because the owner pressed
    a button and is entitled to know it did not work — and because the standard
    set is already available to them as the other half of the choice, so there
    is nothing to quietly substitute.
    """


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _standard_groups(subject_name: str | None) -> list[dict]:
    """The shipped set, shaped like the library, with the name filled in.

    Returned to the owner alongside their own questions so the screen can show
    what 'standard' actually says rather than describing it. Built from the
    constant every time — these are never rows, so there is nothing to keep in
    step.
    """
    return [
        {
            "relationship": group,
            "questions": standard_for(group, subject_name),
        }
        for group in GROUPS
        if group in STANDARD_QUESTIONS
    ]


async def get_library(memoir_id: str, user_id: str) -> dict | None:
    """The whole questions screen. None if the memoir is not this user's.

    Every group is present, including the empty ones, so the screen does not
    have to know the list of groups to render a group with nothing in it.
    """
    async with db() as conn, conn.cursor() as cur:
        memoir = await _owned_with_questions(cur, memoir_id, user_id)
        if memoir is None:
            return None

        await cur.execute(
            """
            SELECT id, relationship::text AS relationship, ordinal, body,
                   source::text AS source, updated_at
              FROM memoir_prompt
             WHERE memoir_id = %(memoir_id)s
             ORDER BY relationship, ordinal
            """,
            {"memoir_id": memoir_id},
        )
        rows = await cur.fetchall()

    held: dict[str, list[dict]] = {group: [] for group in GROUPS}
    for row in rows:
        # A row whose group is not in GROUPS means the enum grew and this file
        # did not. Dropping it from the screen is wrong — it would look
        # deleted — so it goes under 'other', which is where an unknown
        # relationship already belongs.
        held.setdefault(row["relationship"], held["other"]).append(row)

    return {
        "memoir_id": memoir["id"],
        "subject_name": memoir["subject_name"],
        "mode": memoir["questions_mode"],
        "subject_notes": memoir["subject_notes"],
        "groups": [
            {"relationship": group, "questions": held[group]} for group in GROUPS
        ],
        "standard": _standard_groups(memoir["subject_name"]),
    }


async def for_contributor(
    link_token: str,
    participant_token: str | None = None,
    relationship: str | None = None,
) -> dict:
    """The questions one contributor sees. Raises LinkNotUsable if they cannot.

    Neither argument is required, and that is the point: somebody opening the
    link for the first time has no participant token yet, and they are exactly
    the person the questions were written for. Requiring one meant the whole
    library was invisible until after the memory it was supposed to prompt had
    already been left.

    `relationship` is what they have just tapped on the form, which is newer
    than anything stored — so the questions change as they answer. Falling back:
    what they said > what is stored against their token > `other`.

    Reads `questions_mode` rather than checking whether rows exist: an owner
    who chose the standard set and *also* has custom questions saved gets the
    standard set, because that is what they chose. Falling back on emptiness
    would make the mode advisory.

    A group with no custom questions falls through to that group's standard
    set rather than showing nothing. The owner may have deleted a group they
    thought nobody would use, and a cousin arriving to an empty page is the
    failure this whole feature exists to prevent.
    """
    async with db() as conn, conn.cursor() as cur:
        memoir = await contributable_memoir(cur, link_token)
        if memoir is None:
            raise LinkNotUsable

        await cur.execute(
            """
            SELECT subject_name, questions_mode::text AS mode
              FROM memoir
             WHERE id = %(memoir_id)s
            """,
            {"memoir_id": memoir["id"]},
        )
        row = await cur.fetchone()

        group = relationship if relationship in GROUPS else None

        if group is None and participant_token:
            await cur.execute(
                """
                SELECT relationship::text AS relationship
                  FROM memoir_participant
                 WHERE memoir_id = %(memoir_id)s
                   AND contributor_token = %(token)s
                   AND role = 'contributor'
                """,
                {"memoir_id": memoir["id"], "token": participant_token},
            )
            stored = await cur.fetchone()
            # A token that belongs to nobody here is not an error — it is a
            # browser holding a token from another memoir, or a reissued one.
            # They still get questions; they get the ones for "other".
            group = stored["relationship"] if stored else None

        group = group or "other"

        if row["mode"] == "custom":
            await cur.execute(
                """
                SELECT body
                  FROM memoir_prompt
                 WHERE memoir_id = %(memoir_id)s
                   AND relationship = %(relationship)s::relationship_group
                 ORDER BY ordinal
                """,
                {"memoir_id": memoir["id"], "relationship": group},
            )
            custom = [r["body"] for r in await cur.fetchall()]
            if custom:
                return {"relationship": group, "questions": custom}

    return {
        "relationship": group,
        "questions": standard_for(group, row["subject_name"]),
    }


# ---------------------------------------------------------------------------
# Writing a library
# ---------------------------------------------------------------------------


async def generate(
    memoir_id: str, user_id: str, notes: str | None, replace_edited: bool = False
) -> dict | None:
    """Draft a library and save it. None if the memoir is not this user's.

    Raises `NothingGenerated` when the model could not produce one.

    Returns the library as `get_library` would, so the screen re-renders from
    one response rather than generating and then refetching.

    ---------------------------------------------------------------------
    What a reprompt does and does not replace
    ---------------------------------------------------------------------
    By default it replaces the `ai` rows and leaves `owner` rows — the ones
    somebody edited by hand — exactly where they are. Losing a generated
    question costs a model call; losing a hand-written one costs the sentence a
    person chose, and they will not remember it.

    `replace_edited` is the owner saying "start again", and it is theirs to
    say. It is never the default.

    ---------------------------------------------------------------------
    Why the model call sits outside the transaction
    ---------------------------------------------------------------------
    Holding a Postgres connection open across an HTTP call to another service
    is how a bounded pool dies — the same reason signed storage URLs are minted
    outside their transaction in `media_service`, and the same reason assembly
    plans between two transactions rather than inside one.

    The notes are saved in the first transaction, before the call, so a model
    that is unreachable does not also cost the owner the paragraph they typed.
    """
    async with db() as conn, conn.cursor() as cur:
        memoir = await _owned_with_questions(cur, memoir_id, user_id)
        if memoir is None:
            return None

        if notes is not None:
            await cur.execute(
                """
                UPDATE memoir
                   SET subject_notes = %(notes)s, updated_at = now()
                 WHERE id = %(memoir_id)s
                """,
                {"memoir_id": memoir_id, "notes": notes.strip() or None},
            )
            memoir["subject_notes"] = notes.strip() or None

    try:
        library = await gemini.generate(_describe(memoir), Library, system=_SYSTEM)
    except gemini.GeminiDisabled as exc:
        raise NothingGenerated("questions are switched off") from exc
    except gemini.GeminiError as exc:
        logger.warning("Could not draft questions for %s: %s", memoir_id, exc)
        raise NothingGenerated(str(exc)) from exc

    drafted = _clean(library)
    if not drafted:
        raise NothingGenerated("the model returned no usable questions")

    async with db() as conn, conn.cursor() as cur:
        memoir = await _owned_with_questions(cur, memoir_id, user_id)
        if memoir is None:
            return None

        # One transaction for the swap. A library left holding half of one
        # draft and half of another is a screen nobody can make sense of, and
        # `UNIQUE (memoir_id, relationship, ordinal)` would refuse the second
        # half anyway — as a psycopg error, mid-write.
        if replace_edited:
            await cur.execute(
                "DELETE FROM memoir_prompt WHERE memoir_id = %(memoir_id)s",
                {"memoir_id": memoir_id},
            )
        else:
            await cur.execute(
                """
                DELETE FROM memoir_prompt
                 WHERE memoir_id = %(memoir_id)s AND source <> 'owner'
                """,
                {"memoir_id": memoir_id},
            )

        # Where the kept hand-edited questions ended up, so the drafted ones
        # are numbered after them instead of colliding with them.
        await cur.execute(
            """
            SELECT relationship::text AS relationship,
                   COALESCE(MAX(ordinal) + 1, 0) AS next
              FROM memoir_prompt
             WHERE memoir_id = %(memoir_id)s
             GROUP BY relationship
            """,
            {"memoir_id": memoir_id},
        )
        next_ordinal = {r["relationship"]: r["next"] for r in await cur.fetchall()}

        for group, questions in drafted.items():
            ordinal = next_ordinal.get(group, 0)
            for body in questions:
                await cur.execute(
                    """
                    INSERT INTO memoir_prompt
                        (memoir_id, relationship, ordinal, body, source)
                    VALUES
                        (%(memoir_id)s, %(relationship)s::relationship_group,
                         %(ordinal)s, %(body)s, 'ai')
                    """,
                    {
                        "memoir_id": memoir_id,
                        "relationship": group,
                        "ordinal": ordinal,
                        "body": body,
                    },
                )
                ordinal += 1

        # Generating is the owner saying they want these used. Leaving the
        # memoir on 'standard' after drafting a library would mean the work
        # they just watched happen changed nothing a contributor sees.
        await cur.execute(
            """
            UPDATE memoir SET questions_mode = 'custom', updated_at = now()
             WHERE id = %(memoir_id)s
            """,
            {"memoir_id": memoir_id},
        )

    logger.info(
        "Drafted questions for memoir %s across %s groups", memoir_id, len(drafted)
    )
    return await get_library(memoir_id, user_id)


def _describe(memoir: dict) -> str:
    """Everything the model is allowed to know about the subject.

    Labelled lines rather than a sentence, so that what the owner wrote stays
    visibly separate from our framing of it. Prose around somebody's own words
    invites the model to treat our framing as something they said.
    """
    lines = [f"The memoir is about: {memoir['subject_name']}"]

    born, through = memoir["born_year"], memoir["through_year"]
    if born or through:
        lived = f"{born or 'unknown'}–"
        lived += "present" if memoir["subject_is_living"] else str(through or "unknown")
        lines.append(f"Years: {lived}")

    # The free-text label is what the owner typed about themselves and is
    # always better than the enum, which has six values and one is "other".
    relationship = memoir["relationship_label"] or memoir["relationship"]
    if relationship:
        lines.append(f"The person making this memoir is their {relationship}")

    # The answer to "what should never be forgotten about them". Onboarding's
    # last screen promises this "becomes the first question your family is
    # asked", and this line is what makes that sentence true.
    if memoir["never_forget"] and memoir["never_forget"].strip():
        lines.append(
            "The one thing that should never be forgotten about them — build "
            "the first question of every group from this:\n"
            f"{memoir['never_forget'].strip()}"
        )

    if memoir["subject_notes"] and memoir["subject_notes"].strip():
        lines.append(
            "What the family said about them:\n"
            f"{memoir['subject_notes'].strip()[:_MAX_NOTES_CHARS]}"
        )

    return "\n\n".join(lines)


def _clean(library: Library) -> dict[str, list[str]]:
    """The drafted library, reduced to what is safe to store.

    Dropped: unknown groups, blank questions, duplicates within a group, and
    anything past `MAX_PER_GROUP`. A group that comes back with fewer than
    `MIN_PER_GROUP` is kept as-is rather than padded — three good questions
    beat five with two invented to hit a number.
    """
    cleaned: dict[str, list[str]] = {}

    for group in library.groups:
        name = (group.relationship or "").strip()
        if name not in STANDARD_QUESTIONS:
            logger.info("Dropped a drafted group that is not a relationship")
            continue

        seen: list[str] = []
        for question in group.questions:
            body = (question or "").strip()
            if not body or body in seen:
                continue
            seen.append(body)
            if len(seen) == MAX_PER_GROUP:
                break

        if seen:
            cleaned[name] = seen

    return cleaned


# ---------------------------------------------------------------------------
# Editing one question
# ---------------------------------------------------------------------------


async def update_question(prompt_id: str, user_id: str, body: str) -> dict | None:
    """Rewrite one question by hand. None if it is not this user's.

    Flips `source` to 'owner', which is the whole mechanism behind "a reprompt
    keeps what you wrote". Nothing else in the system sets that value.

    Ownership is reached by joining up to `memoir`, the same way
    `owned_memoir_of_memory` does for a memory addressed by its own id.
    """
    async with db() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE memoir_prompt p
               SET body = %(body)s, source = 'owner', updated_at = now()
              FROM memoir m
             WHERE p.id = %(prompt_id)s
               AND m.id = p.memoir_id
               AND m.created_by_user_id = %(user_id)s
         RETURNING p.id, p.relationship::text AS relationship, p.ordinal,
                   p.body, p.source::text AS source, p.updated_at
            """,
            {"prompt_id": prompt_id, "user_id": user_id, "body": body.strip()},
        )
        return await cur.fetchone()


async def delete_question(prompt_id: str, user_id: str) -> bool:
    """Remove one question. True if there was one of theirs to remove.

    The gap it leaves in `ordinal` is deliberate and harmless: order is read
    with `ORDER BY ordinal`, which does not care whether the numbers are
    contiguous. Renumbering the rest would rewrite rows nobody touched.
    """
    async with db() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            DELETE FROM memoir_prompt p
             USING memoir m
             WHERE p.id = %(prompt_id)s
               AND m.id = p.memoir_id
               AND m.created_by_user_id = %(user_id)s
         RETURNING p.id
            """,
            {"prompt_id": prompt_id, "user_id": user_id},
        )
        return await cur.fetchone() is not None


async def set_mode(memoir_id: str, user_id: str, mode: str) -> dict | None:
    """Choose which set of questions contributors see. None if not their memoir.

    Switching to 'standard' does not delete the custom questions. The owner is
    choosing what is used, not throwing away what was written, and switching
    back must return the library they had — otherwise the choice is one-way and
    nobody would risk making it.
    """
    async with db() as conn, conn.cursor() as cur:
        memoir = await owned_memoir(cur, memoir_id, user_id)
        if memoir is None:
            return None

        await cur.execute(
            """
            UPDATE memoir
               SET questions_mode = %(mode)s::questions_mode, updated_at = now()
             WHERE id = %(memoir_id)s
            """,
            {"memoir_id": memoir_id, "mode": mode},
        )

    return await get_library(memoir_id, user_id)


# ---------------------------------------------------------------------------
# Small shared pieces
# ---------------------------------------------------------------------------


async def _owned_with_questions(cur, memoir_id: str, user_id: str) -> dict | None:
    """The memoir with everything the library needs, if this user owns it.

    Not `owned_memoir()`, which returns three columns. This needs the whole
    subject — the years, the owner's relationship, `never_forget` and the notes
    — because those are the raw material a question is written from.

    `never_forget` is selected here and must never leave this module in a
    response. It is the owner's private answer, and `LinkInvitation` and
    `MemoirReading` both exclude it for the same reason: a link gets forwarded.
    It reaches the model, which is what onboarding promised, and it reaches no
    contributor.
    """
    await cur.execute(
        """
        SELECT id, subject_name, born_year, through_year, subject_is_living,
               never_forget, subject_notes,
               questions_mode::text AS questions_mode,
               status::text AS status,
               (SELECT p.relationship::text
                  FROM memoir_participant p
                 WHERE p.memoir_id = memoir.id AND p.role = 'owner') AS relationship,
               (SELECT p.relationship_label
                  FROM memoir_participant p
                 WHERE p.memoir_id = memoir.id AND p.role = 'owner')
                   AS relationship_label
          FROM memoir
         WHERE id = %(memoir_id)s
           AND created_by_user_id = %(user_id)s
        """,
        {"memoir_id": memoir_id, "user_id": user_id},
    )
    return await cur.fetchone()
