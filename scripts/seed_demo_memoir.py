"""Fill a memoir with material worth assembling, then assemble it.

    python scripts/seed_demo_memoir.py --memoir <uuid> --subject "Nasreen Fatima"
    python scripts/seed_demo_memoir.py --memoir <uuid> --plan

Why this exists: every screen after `/archive` needs an archive behind it, and
clicking one in by hand is twenty minutes before you can look at the thing you
changed. `tests/factories.py` does this for the test database, which is
TRUNCATEd between tests and unreachable from the running app.

Three rules it keeps, because it points at whatever database `.env` names:

  * it **never deletes**. Existing memories, participants and chapters are left
    exactly as they are; everything written is added.
  * it is **idempotent by name**. A contributor is matched on `display_name`
    and a memory on its first line, so running it twice does not double the
    archive.
  * it **refuses a published memoir**. Publication is immutable, and a seed
    script is not the exception to that.

The material below is fiction, written for this file. It is deliberately not
"lorem ipsum": the assembly step reads it and decides where a life divides, so
placeholder text produces a placeholder book and tells you nothing about
whether the feature works. Two contributors describe the same afternoon
differently on purpose — that is the divergent-accounts rule, and it has to be
visible in a demo archive or nobody will notice when it breaks.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.logging_config import setup_logging  # noqa: E402
from src.domain.chapters.assembly_service import assemble, generate_plan  # noqa: E402
from src.integrations.db import close_pool, db, open_pool  # noqa: E402

logger = logging.getLogger("seed")

SUBJECT = {"born_year": 1938, "through_year": 2019, "subject_is_living": False}

# (display_name, relationship_group, relationship_label)
#
# `relationship_group` is a closed enum — child, grandchild, spouse_partner,
# friend, self, other — and it is what the question library is keyed on. A
# sister and a student are both `other`, which means *unstated group*, not
# unstated person: the label beside it is what the reader's credit line prints,
# through `COALESCE(relationship_label, relationship::text)`.
PEOPLE = [
    ("Razia Bano", "other", "Her older sister"),
    ("Imran Qadir", "child", "Her son"),
    ("Shahnaz Malik", "friend", "Neighbour on Jail Road"),
    ("Aliya Naqvi", "other", "Student, 1979"),
    ("Farhan Sheikh", "grandchild", "Her grandson"),
]

# (contributor, happened_on, title, body_text)
MEMORIES = [
    (
        "Razia Bano",
        "1946-06-02",
        None,
        "We shared a charpoy on the roof in the summer because the house held "
        "the heat until midnight. She would not sleep until she had named "
        "every sound in the street — the cart, the dog at the corner, the man "
        "who sold ice. I used to think she was frightened. She was taking "
        "attendance.",
    ),
    (
        "Razia Bano",
        "1949-11-19",
        "The harmonium",
        "Our father brought home a harmonium with two broken reeds and she "
        "played it anyway, for four years, wrong. Nobody corrected her. By the "
        "time it was repaired she had learned the wrong notes so thoroughly "
        "that the right ones sounded flat to her.",
    ),
    (
        "Razia Bano",
        "1953-03-08",
        None,
        "She was fifteen when she started teaching the neighbours' children "
        "for nothing, on the veranda, after school. Our mother disapproved and "
        "made tea for all of them anyway.",
    ),
    (
        "Shahnaz Malik",
        "1961-08-14",
        None,
        "I moved onto Jail Road the year my husband was posted there and she "
        "was the first person to knock. She brought rice and stayed three "
        "hours. I did not know then that this was simply what she did, and "
        "that half the street had the same story about the day they arrived.",
    ),
    (
        "Imran Qadir",
        "1964-04-27",
        None,
        "She married my father in April and kept her own name on everything "
        "she signed, which in 1964 required a certain amount of explaining "
        "each time. She explained it each time.",
    ),
    (
        "Imran Qadir",
        "1971-12-16",
        "The winter of the blackout",
        "During the blackout we taped the windows and she carried on the "
        "lessons by candle, six children round the table, because she said "
        "the alternative was six children listening to the radio with their "
        "parents. I was four and I remember the smell of the tape.",
    ),
    (
        "Shahnaz Malik",
        "1971-12-18",
        None,
        "People say she was calm that winter. She was not calm. She was "
        "steady, which is a different thing and cost her more.",
    ),
    (
        "Aliya Naqvi",
        "1979-09-03",
        None,
        "She taught me in the second year and she had one exercise: play it "
        "once, then play it as though you had forgotten it. That was the whole "
        "first term. Eleven of us have told this same story to each other over "
        "the years, in almost the same words.",
    ),
    (
        "Aliya Naqvi",
        "1982-05-20",
        None,
        "She refused to sit on the examination board after 1982 and never gave "
        "the department a reason. I asked her once, years later. She said, "
        "“I am not the right person to decide who is finished.”",
    ),
    (
        "Imran Qadir",
        "1986-07-11",
        None,
        "The school at Ichhra was two rooms and eighty children and she was "
        "there for eleven years. My father drove her at seven and collected "
        "her at five and read in the car, because there was nowhere in Ichhra "
        "to wait.",
    ),
    (
        "Farhan Sheikh",
        "1998-10-05",
        None,
        "My earliest memory of her is being taught to fold a paper boat "
        "properly, with the fold pressed twice, and being told that anything "
        "worth doing has a second fold in it. I was five. I have never once "
        "folded a boat since without hearing it.",
    ),
    (
        "Farhan Sheikh",
        "2004-02-22",
        "The blue tin",
        "She kept every letter in a blue biscuit tin with a rubber band round "
        "it, sorted by nothing at all. When I offered to put them in order she "
        "said the order was the point — that she wanted to be surprised by her "
        "own life when she opened it.",
    ),
    (
        "Razia Bano",
        "2011-01-30",
        None,
        "After our brother died she stopped playing entirely for two years and "
        "then started again on a Tuesday afternoon with nothing said about it. "
        "I was in the next room. I did not go in.",
    ),
    (
        "Imran Qadir",
        "2016-06-09",
        None,
        "She catalogued the town library's Urdu manuscripts for the last "
        "eleven years of her life, unpaid, in a hand so precise the librarian "
        "thought it was typeset. Nobody asked her to start and nobody could "
        "have made her stop.",
    ),
    (
        "Farhan Sheikh",
        "2019-11-14",
        None,
        "She died at home on a Thursday, having spent the Wednesday labelling "
        "boxes. The labels are in her father's handwriting, more or less "
        "exactly, which none of us noticed until we were emptying the room.",
    ),
]


# Which seeded memories a photograph belongs to, if there are photographs to
# go round. Matched on the first words of the body, like everything else here.
PHOTO_HOMES = [
    "Our father brought home a harmonium",
    "During the blackout we taped the windows",
    "She kept every letter in a blue biscuit tin",
    "The school at Ichhra was two rooms",
]


async def _memoir(cur, memoir_id: str) -> dict:
    await cur.execute(
        """
        SELECT m.id, m.subject_name, m.status::text AS status, u.email
          FROM memoir m
          LEFT JOIN user_account u ON u.id = m.created_by_user_id
         WHERE m.id = %(id)s
        """,
        {"id": memoir_id},
    )
    row = await cur.fetchone()
    if row is None:
        raise SystemExit(f"No memoir {memoir_id} in this database.")
    if row["status"] == "published":
        raise SystemExit(
            "That memoir is published. Publication is immutable — seed a draft."
        )
    return row


async def _participant(cur, memoir_id: str, name: str, group: str, label: str) -> str:
    """The contributor, made if they are not there yet. Matched on the name."""
    await cur.execute(
        """
        SELECT id FROM memoir_participant
         WHERE memoir_id = %(memoir)s AND display_name = %(name)s
         LIMIT 1
        """,
        {"memoir": memoir_id, "name": name},
    )
    found = await cur.fetchone()
    if found is not None:
        return str(found["id"])

    await cur.execute(
        """
        INSERT INTO memoir_participant
            (memoir_id, role, display_name, relationship, relationship_label,
             first_opened_at, contributor_token)
        VALUES (%(memoir)s, 'contributor', %(name)s,
                %(group)s::relationship_group, %(label)s,
                now(), encode(gen_random_bytes(24), 'hex'))
     RETURNING id
        """,
        {"memoir": memoir_id, "name": name, "group": group, "label": label},
    )
    return str((await cur.fetchone())["id"])


async def seed(memoir_id: str, subject: str | None) -> None:
    async with db() as conn, conn.cursor() as cur:
        memoir = await _memoir(cur, memoir_id)
        logger.info(
            "Seeding %s (%s), owned by %s",
            memoir["subject_name"],
            memoir["status"],
            memoir["email"],
        )

        if subject:
            await cur.execute(
                """
                UPDATE memoir
                   SET subject_name       = %(name)s,
                       born_year          = %(born)s,
                       through_year       = %(through)s,
                       subject_is_living  = %(living)s,
                       updated_at         = now()
                 WHERE id = %(id)s
                """,
                {
                    "id": memoir_id,
                    "name": subject,
                    "born": SUBJECT["born_year"],
                    "through": SUBJECT["through_year"],
                    "living": SUBJECT["subject_is_living"],
                },
            )
            logger.info("Subject is now %s, %s–%s", subject, 1938, 2019)

        people = {
            name: await _participant(cur, memoir_id, name, group, label)
            for name, group, label in PEOPLE
        }

        added = 0
        for name, happened_on, title, body in MEMORIES:
            # Matched on the opening words rather than on a marker column: the
            # schema has nowhere to record "this was seeded", and adding one
            # for a script's convenience is the wrong direction.
            await cur.execute(
                """
                SELECT 1 FROM memory
                 WHERE memoir_id = %(memoir)s
                   AND left(body_text, 40) = %(head)s
                 LIMIT 1
                """,
                {"memoir": memoir_id, "head": body[:40]},
            )
            if await cur.fetchone() is not None:
                continue

            await cur.execute(
                """
                INSERT INTO memory (memoir_id, participant_id, kind, title,
                                    body_text, happened_on)
                VALUES (%(memoir)s, %(participant)s, 'text', %(title)s,
                        %(body)s, %(happened)s)
                """,
                {
                    "memoir": memoir_id,
                    "participant": people[name],
                    "title": title,
                    "body": body,
                    "happened": happened_on,
                },
            )
            added += 1

        logger.info(
            "%d contributors, %d memories added (%d already there)",
            len(people),
            added,
            len(MEMORIES) - added,
        )


async def adopt_photos(memoir_id: str) -> None:
    """Re-attach photographs already in this archive to memories with words.

    The one thing here that changes an existing row, and it is opt-in for that
    reason. Nothing is uploaded and nothing is generated: a made-up photograph
    in a memoir is exactly what the fabrication rule forbids, and
    `media_asset.storage_path` is UNIQUE so a second row cannot point at an
    object either.

    What it does instead is move an image off a memory that carries no usable
    prose — a stray upload, a test row — and onto a seeded memory that does.
    That matters because the planner can only place a photograph beside a
    paragraph, and a paragraph only exists for a memory with words in it. Left
    alone, an archive of dated prose and orphaned images assembles with zero
    figures and the whole vision path looks broken when it is behaving.
    """
    async with db() as conn, conn.cursor() as cur:
        await _memoir(cur, memoir_id)

        # Images whose memory has nothing a chapter could quote. `body_text`
        # under ~25 characters is the heuristic: real memories here are
        # paragraphs, and the rows this is aimed at hold "dsdad".
        await cur.execute(
            """
            SELECT a.id, a.memory_id
              FROM media_asset a
              JOIN memory m ON m.id = a.memory_id
             WHERE a.memoir_id = %(memoir)s
               AND a.kind = 'image'
               AND a.uploaded_at IS NOT NULL
               AND length(btrim(coalesce(m.body_text, ''))) < 25
             ORDER BY a.created_at
            """,
            {"memoir": memoir_id},
        )
        orphans = await cur.fetchall()
        if not orphans:
            logger.info("No stray photographs to re-attach — nothing to do")
            return

        moved = 0
        for asset, head in zip(orphans, PHOTO_HOMES):
            await cur.execute(
                """
                SELECT id FROM memory
                 WHERE memoir_id = %(memoir)s
                   AND body_text LIKE %(head)s
                 LIMIT 1
                """,
                {"memoir": memoir_id, "head": head + "%"},
            )
            home = await cur.fetchone()
            if home is None:
                continue

            await cur.execute(
                """
                UPDATE media_asset
                   SET memory_id = %(memory)s
                 WHERE id = %(asset)s
                """,
                {"asset": asset["id"], "memory": home["id"]},
            )
            moved += 1

        logger.info(
            "%d photograph(s) re-attached to memories that have words", moved
        )


async def build(memoir_id: str) -> None:
    """Plan the memoir and assemble it, the way the two buttons do."""
    async with db() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT created_by_user_id FROM memoir WHERE id = %(id)s",
            {"id": memoir_id},
        )
        row = await cur.fetchone()
        if row is None:
            raise SystemExit(f"No memoir {memoir_id}.")
        user_id = str(row["created_by_user_id"])

    logger.info("Planning — this is the slow one, it reads the whole archive")
    plan = await generate_plan(memoir_id, user_id)
    logger.info(
        "Organised by %s, into %d chapters: %s",
        plan["organised_by"],
        len(plan["chapters"]),
        ", ".join(chapter["title"] for chapter in plan["chapters"]),
    )

    result = await assemble(memoir_id, user_id)
    logger.info(
        "Assembled: %d chapters, %d passages, %d sources, %d photographs",
        result["chapters"],
        result["blocks"],
        result["sources"],
        result["figures"],
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memoir", required=True, help="the memoir's uuid")
    parser.add_argument("--subject", help="rename the subject and set the years")
    parser.add_argument(
        "--plan",
        action="store_true",
        help="plan and assemble afterwards (spends a Gemini call)",
    )
    parser.add_argument(
        "--adopt-photos",
        action="store_true",
        help="move stray photographs onto seeded memories, so figures can place",
    )
    parser.add_argument(
        "--only-build",
        action="store_true",
        help="skip seeding; plan and assemble what is already there",
    )
    args = parser.parse_args()

    setup_logging()
    await open_pool()
    try:
        if not args.only_build:
            await seed(args.memoir, args.subject)
        if args.adopt_photos:
            await adopt_photos(args.memoir)
        if args.plan or args.only_build:
            await build(args.memoir)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
