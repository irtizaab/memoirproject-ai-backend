# The owner's conversation with the guide about the plan.
#
# Three functions, all owner-only. `send` is the one with a shape worth
# explaining: it is a message exchange that may, in the middle, edit the plan,
# run the whole planner, and write the book — so it is short transactions
# around model calls rather than one of anything, for the same reason
# `generate_plan` is two: a Postgres connection is not held open across a
# request to a model.
#
# What the guide sees is decided in `planner.chat`, and it is never a memory's
# words. This file only decides what is stored, and stores it.

import logging

from src.domain.chapters import assembly_service, plan_service, planner
from src.domain.chapters.assembly_service import MemoirSealed
from src.domain.memoirs.access import owned_memoir
from src.integrations.db import db

logger = logging.getLogger(__name__)

# What the guide says when it cannot say anything. Stored as a real message so
# the owner's question is not left hanging unanswered in the transcript.
_SILENCE = "I could not answer just now. Ask again in a moment."

# How much of the conversation the guide is shown.
_HISTORY = 20


async def list_messages(memoir_id: str, user_id: str) -> list[dict] | None:
    """Every message, oldest first. None if the memoir is not theirs."""
    async with db() as conn, conn.cursor() as cur:
        if await owned_memoir(cur, memoir_id, user_id) is None:
            return None
        return await _messages(cur, memoir_id, limit=None)


async def send(memoir_id: str, user_id: str, body: str) -> dict | None:
    """The owner says something; the guide answers, and may change the book.

    Two kinds of change, both applied here and both followed by `assemble`,
    so the book the owner opens is the one they just talked about:

      outline   rename, reorder, drop — `plan_service.edit`, the same path
                `PATCH /plan` takes, with the guide's chapter numbers mapped
                back to ids here
      replan    the builder runs again on the request restated

    Returns `{"reply": message, "plan": plan | None}` — the plan whenever it
    changed, so the frontend can swap the outline without a second request.
    None if the memoir is not theirs.

    Raises `MemoirSealed`: a published memoir has no plan to talk about.
    """
    async with db() as conn, conn.cursor() as cur:
        memoir = await owned_memoir(cur, memoir_id, user_id)
        if memoir is None:
            return None
        if memoir["status"] == "published":
            raise MemoirSealed

        await cur.execute(
            """
            INSERT INTO memoir_chat_message (memoir_id, role, body)
            VALUES (%(memoir_id)s, 'owner', %(body)s)
            """,
            {"memoir_id": memoir_id, "body": body},
        )
        history = await _messages(cur, memoir_id, limit=_HISTORY)
        row = await plan_service.load(cur, memoir_id)
        memories = await assembly_service._memories(cur, memoir_id)

    plan = plan_service.summarise(row) if row else None
    shape = planner._shape(memories, memoir)
    # The message just stored is the last in `history`; the guide is handed
    # it separately as the thing to answer.
    turn = await planner.chat(shape, plan, history[:-1], body)

    reply, changed = _SILENCE, False
    if turn is not None:
        reply = turn.reply.strip()[:4000] or _SILENCE
        if turn.outline and plan is not None:
            changed, note = await _apply_outline(memoir_id, user_id, plan, turn.outline)
            if note:
                reply = f"{reply}\n\n{note}"
        elif turn.replan:
            try:
                changed = (
                    await assembly_service.generate_plan(
                        memoir_id, user_id, turn.instructions_for_builder or body
                    )
                    is not None
                )
            except assembly_service.NothingToAssemble:
                reply = "There is nothing in the archive to plan from yet."

    new_plan = None
    if changed:
        # Planning and building are one step to the owner. A changed outline
        # is written into the book here, not left for a second button.
        try:
            await assembly_service.assemble(memoir_id, user_id)
        except (assembly_service.NothingToAssemble, plan_service.NoPlanYet) as exc:
            logger.info("Chat changed the plan but could not assemble: %s", exc)
        new_plan = await plan_service.read(memoir_id, user_id)
        # The guide wrote its reply before anything ran, so it can only
        # promise. What actually came of it — the outline as it now stands,
        # with the note the planner wrote or the reason it fell back — is
        # appended, so a plan that could not be made says so here and not
        # only in the server log.
        if new_plan:
            reply = f"{reply}\n\n{describe(new_plan)}"
    reply = reply[:4000]

    async with db() as conn, conn.cursor() as cur:
        if await owned_memoir(cur, memoir_id, user_id) is None:
            return None
        await cur.execute(
            """
            INSERT INTO memoir_chat_message (memoir_id, role, body, replanned)
            VALUES (%(memoir_id)s, 'guide', %(body)s, %(replanned)s)
         RETURNING id, role, body, replanned, created_at
            """,
            {"memoir_id": memoir_id, "body": reply, "replanned": changed},
        )
        stored = await cur.fetchone()

    return {"reply": stored, "plan": new_plan}


async def announce(memoir_id: str, user_id: str, plan: dict) -> None:
    """The outline the build produced, as a message from the guide.

    Called by the plan route, so a book built from the button and one built
    from the conversation read the same way afterwards: the outline is a
    thing the guide said, in the transcript, not a panel beside it.
    """
    async with db() as conn, conn.cursor() as cur:
        if await owned_memoir(cur, memoir_id, user_id) is None:
            return
        await cur.execute(
            """
            INSERT INTO memoir_chat_message (memoir_id, role, body, replanned)
            VALUES (%(memoir_id)s, 'guide', %(body)s, true)
            """,
            {"memoir_id": memoir_id, "body": describe(plan)[:4000]},
        )


def describe(plan: dict) -> str:
    """A plan in words: the note, the numbered chapters, what the reviewer found.

    The numbers are the ones the guide will be shown, so the owner and the
    guide are talking about the same "chapter 3".
    """
    lines: list[str] = []
    note = plan.get("guide") or (
        plan.get("reason") if plan["organised_by"] != "planner" else None
    )
    if note:
        lines.append(note)
    if plan["organised_by"] != "planner":
        lines.append("The book is divided by decade instead.")
    lines.append("")
    for number, chapter in enumerate(plan["chapters"], start=1):
        parts = []
        if chapter.get("from_year"):
            through = chapter.get("through_year")
            parts.append(
                f"{chapter['from_year']}–{through}"
                if through and through != chapter["from_year"]
                else str(chapter["from_year"])
            )
        blocks = len(chapter.get("blocks", []))
        parts.append(f"{blocks} passage{'' if blocks == 1 else 's'}")
        figures = len(chapter.get("figures", []))
        if figures:
            parts.append(f"{figures} photograph{'' if figures == 1 else 's'}")
        lines.append(f"{number}. {chapter['title']} — {' · '.join(parts)}")
    findings = (plan.get("review") or {}).get("findings") or []
    if findings:
        lines.append("")
        lines.append("What the reviewer found:")
        for finding in findings:
            where = f"{finding['chapter']} — " if finding.get("chapter") else ""
            fixed = " (changed)" if finding.get("fixed") else ""
            lines.append(f"• {where}{finding['note']}{fixed}")
    return "\n".join(lines).strip()


async def _apply_outline(
    memoir_id: str, user_id: str, plan: dict, outline: list
) -> tuple[bool, str | None]:
    """The guide's numbered outline, applied. (changed, a note for the owner).

    Numbers are positions in the outline the guide was shown; anything else
    is refused here before `plan_service.edit` sees it, and the owner is told
    the outline was left alone rather than shown a half-applied one.
    """
    by_number = {n: c for n, c in enumerate(plan["chapters"], start=1)}
    chapters = []
    for wanted in outline:
        chapter = by_number.get(wanted.number)
        if chapter is None:
            return False, "I could not change the outline that way; it is as it was."
        # The stored chapter whole, with its title changed: `edit` reads a
        # chapter's passages from what is submitted, so a bare title would
        # read as "keep nothing of it".
        chapters.append({**chapter, "title": wanted.title.strip() or chapter["title"]})
    try:
        edited = await plan_service.edit(memoir_id, user_id, chapters)
    except (plan_service.UnknownChapter, plan_service.NothingLeft, plan_service.NoPlanYet):
        return False, "I could not change the outline that way; it is as it was."
    return edited is not None, None


async def _messages(cur, memoir_id: str, limit: int | None) -> list[dict]:
    """Messages oldest first; the most recent `limit` of them when given."""
    await cur.execute(
        """
        SELECT id, role, body, replanned, created_at
          FROM (SELECT id, role, body, replanned, created_at
                  FROM memoir_chat_message
                 WHERE memoir_id = %(memoir_id)s
                 ORDER BY created_at DESC, id DESC
                 LIMIT %(limit)s) AS recent
         ORDER BY created_at ASC, id ASC
        """,
        {"memoir_id": memoir_id, "limit": limit},
    )
    return await cur.fetchall()
