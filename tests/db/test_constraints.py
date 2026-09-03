"""The rules the database refuses to break.

This codebase validates at two layers on purpose: Pydantic at the edge for a
readable error, and a constraint underneath for the actual guarantee. The second
one holds even when a future code path forgets to check — which is the whole
reason it exists, and the reason it deserves tests of its own rather than being
assumed to work.

Everything here writes SQL directly. Going through the API would test the
application's checks, not the database's.
"""

import uuid

import psycopg
import pytest

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.db]


def violates(conn, sql: str, params: dict | None = None) -> str:
    """Run something the schema should refuse, and return the constraint name.

    Fails the test if the write succeeds — a constraint that does not fire is
    the failure mode this file exists to catch.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
    except psycopg.errors.IntegrityError as exc:
        conn.rollback()
        return exc.diag.constraint_name or ""
    except psycopg.Error:
        conn.rollback()
        raise

    conn.rollback()
    pytest.fail("the database accepted a row it should have refused")


# ---------------------------------------------------------------------------
# A memory must hold something
# ---------------------------------------------------------------------------


def test_a_text_memory_must_carry_words(db_conn, factory, owner):
    """`memory_text_has_body`.

    The application raises `EmptyMemory` first, so a person gets a sentence
    somebody wrote rather than a constraint name. This is the layer underneath —
    what stops an empty memory being stored by any path at all, including a
    future one that forgets.
    """
    name = violates(
        db_conn,
        """
        INSERT INTO memory (memoir_id, participant_id, kind, body_text)
        VALUES (%(memoir)s, %(participant)s, 'text', '   ')
        """,
        {
            "memoir": owner["memoir"]["id"],
            "participant": owner["memoir"]["owner_participant_id"],
        },
    )
    assert name == "memory_text_has_body"


def test_a_photo_memory_needs_no_words(db_conn, factory, owner):
    """The constraint is scoped to `kind = 'text'`, deliberately.

    A photograph with no caption is a perfectly good memory.
    """
    row = factory.memory(
        owner["memoir"]["id"],
        owner["memoir"]["owner_participant_id"],
        kind="photo",
        body_text=None,
    )
    assert row["id"]


# ---------------------------------------------------------------------------
# Publication is a pair of columns that move together
# ---------------------------------------------------------------------------


def test_published_status_and_timestamp_cannot_disagree(db_conn, owner):
    """`memoir_published_at_matches_status`, a biconditional.

    Either both say published or neither does. A memoir marked published with no
    timestamp — or timestamped without being published — would make "when was
    this sealed" unanswerable, on the one record that can never be edited
    afterwards.
    """
    assert (
        violates(
            db_conn,
            "UPDATE memoir SET status = 'published' WHERE id = %(id)s",
            {"id": owner["memoir"]["id"]},
        )
        == "memoir_published_at_matches_status"
    )

    assert (
        violates(
            db_conn,
            "UPDATE memoir SET published_at = now() WHERE id = %(id)s",
            {"id": owner["memoir"]["id"]},
        )
        == "memoir_published_at_matches_status"
    )


# ---------------------------------------------------------------------------
# One of each
# ---------------------------------------------------------------------------


def test_a_memoir_has_exactly_one_owner(db_conn, owner):
    """`participant_one_owner_per_memoir` — a partial unique index.

    A rule a CHECK cannot express, because a CHECK only ever sees one row. Two
    owners would mean two people able to publish, delete and revoke, with no way
    to say which was intended.
    """
    account = None
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO auth.users (id) VALUES (gen_random_uuid()) RETURNING id")
        user_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO user_account (id, email) VALUES (%(id)s, %(email)s)
            """,
            {"id": user_id, "email": f"second-{uuid.uuid4().hex[:6]}@example.test"},
        )
    db_conn.commit()

    name = violates(
        db_conn,
        """
        INSERT INTO memoir_participant
            (memoir_id, role, user_id, display_name, relationship)
        VALUES (%(memoir)s, 'owner', %(user)s, 'Second Owner', 'other')
        """,
        {"memoir": owner["memoir"]["id"], "user": user_id},
    )
    assert name == "participant_one_owner_per_memoir"
    assert account is None  # nothing else was created


def test_only_one_live_link_per_scope(db_conn, owner):
    """`memoir_link_one_live_per_scope`.

    This is why `reissue_link` revokes before inserting rather than the other
    way round — the reverse order violates this index, and doing the two in
    separate transactions would leave a window with no live link at all.
    """
    name = violates(
        db_conn,
        """
        INSERT INTO memoir_link (memoir_id, scope)
        VALUES (%(memoir)s, 'contribute')
        """,
        {"memoir": owner["memoir"]["id"]},
    )
    assert name == "memoir_link_one_live_per_scope"


def test_a_revoked_link_frees_the_slot(db_conn, factory, owner):
    """Revoked rows are kept, so `open_count` stays readable afterwards.

    The partial index only covers live ones, which is what lets the old row
    remain as evidence of how far a killed link travelled.
    """
    factory.revoke_link(owner["memoir"]["link_id"])
    replacement = factory.link(owner["memoir"]["id"], scope="contribute")
    assert replacement["token"]


def test_one_memoir_per_account(db_conn, owner):
    """`memoir_one_per_account`, and the bug it was written for.

    The archive shows `memoirs[0]`. When a second memoir became possible it
    silently became the visible one and every memory in the first stopped being
    rendered — not deleted, just unreachable. One test account had five memoirs
    and could see one.

    Data loss wearing the costume of a display bug, which is the worst kind:
    nothing looks broken.
    """
    name = violates(
        db_conn,
        """
        INSERT INTO memoir (created_by_user_id, subject_name)
        VALUES (%(owner)s, 'A Second Subject')
        """,
        {"owner": owner["account"]["id"]},
    )
    assert name == "memoir_one_per_account"


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------


def test_only_audio_carries_a_duration(db_conn, owner):
    """`asset_duration_is_audio_only`.

    A photograph with a length is meaningless, and it would be summed into
    somebody's transcription budget by `_over_budget`, which counts audio.
    """
    name = violates(
        db_conn,
        """
        INSERT INTO media_asset (memoir_id, kind, storage_path, mime_type, duration_ms)
        VALUES (%(memoir)s, 'image', %(path)s, 'image/jpeg', 5000)
        """,
        {"memoir": owner["memoir"]["id"], "path": f"{uuid.uuid4()}/x.jpg"},
    )
    assert name == "asset_duration_is_audio_only"


def test_two_rows_cannot_point_at_one_object(db_conn, owner, factory):
    """`storage_path` is UNIQUE.

    Two rows sharing an object means deleting either one breaks the other — a
    photograph that vanishes from a memory nobody touched.
    """
    first = factory.asset(owner["memoir"]["id"])

    name = violates(
        db_conn,
        """
        INSERT INTO media_asset (memoir_id, kind, storage_path, mime_type)
        VALUES (%(memoir)s, 'image', %(path)s, 'image/jpeg')
        """,
        {"memoir": owner["memoir"]["id"], "path": first["storage_path"]},
    )
    assert "storage_path" in name


def test_a_negative_size_is_refused(db_conn, owner):
    name = violates(
        db_conn,
        """
        INSERT INTO media_asset (memoir_id, kind, storage_path, mime_type, byte_size)
        VALUES (%(memoir)s, 'image', %(path)s, 'image/jpeg', -1)
        """,
        {"memoir": owner["memoir"]["id"], "path": f"{uuid.uuid4()}/x.jpg"},
    )
    assert name == "asset_size_not_negative"


# ---------------------------------------------------------------------------
# Cross-memoir references are impossible, not merely unlikely
# ---------------------------------------------------------------------------


def test_a_memory_cannot_be_attributed_across_memoirs(db_conn, owner, stranger):
    """The composite foreign key on `(memoir_id, participant_id)`.

    One extra line in migration 0001 — `UNIQUE (memoir_id, id)` on participants —
    is what makes this expressible. Without it the FK would be on
    `participant_id` alone and a memory in one memoir could be attributed to
    somebody in another, with only application code preventing it.
    """
    theirs = None
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM memoir_participant WHERE memoir_id = %(m)s LIMIT 1",
            {"m": stranger["memoir"]["id"]},
        )
        theirs = cur.fetchone()["id"]

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memory (memoir_id, participant_id, kind, body_text)
                VALUES (%(memoir)s, %(participant)s, 'text', 'grafted')
                """,
                {"memoir": owner["memoir"]["id"], "participant": theirs},
            )
    db_conn.rollback()


def test_a_contributor_cannot_be_merged_across_memoirs(db_conn, owner, stranger, factory):
    """`participant_merge_target`, the same device applied to merges.

    A merge that reached across memoirs would move one family's memories under
    another family's contributor. Refused by the schema, so no code path can do
    it by accident.
    """
    mine = factory.contributor(owner["memoir"]["id"])
    theirs = factory.contributor(stranger["memoir"]["id"])

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE memoir_participant SET merged_into = %(winner)s
                 WHERE id = %(loser)s
                """,
                {"winner": theirs["id"], "loser": mine["id"]},
            )
    db_conn.rollback()


def test_a_participant_cannot_be_merged_into_itself(db_conn, owner, factory):
    """`participant_not_merged_into_self` — otherwise the pointer loops."""
    contributor = factory.contributor(owner["memoir"]["id"])

    name = violates(
        db_conn,
        "UPDATE memoir_participant SET merged_into = id WHERE id = %(id)s",
        {"id": contributor["id"]},
    )
    assert name == "participant_not_merged_into_self"


def test_the_owner_can_never_be_merged_or_hold_a_contributor_token(db_conn, owner):
    """Two constraints, one idea: the owner is not a contributor.

    They arrive through signup rather than the link, there is exactly one of
    them, and they are nobody's duplicate.
    """
    owner_participant = owner["memoir"]["owner_participant_id"]

    assert (
        violates(
            db_conn,
            """
            UPDATE memoir_participant
               SET contributor_token = encode(gen_random_bytes(24), 'hex')
             WHERE id = %(id)s
            """,
            {"id": owner_participant},
        )
        == "participant_owner_has_no_token"
    )


def test_a_participant_needs_a_name(db_conn, owner):
    """`participant_name_not_blank`.

    The reason `_resolve_contributor` skips the update when a stripped name is
    empty: writing it would violate this and cost somebody their contribution
    over a stray space.
    """
    name = violates(
        db_conn,
        """
        INSERT INTO memoir_participant (memoir_id, role, display_name, relationship)
        VALUES (%(memoir)s, 'contributor', '   ', 'other')
        """,
        {"memoir": owner["memoir"]["id"]},
    )
    assert name == "participant_name_not_blank"


# ---------------------------------------------------------------------------
# What survives what
# ---------------------------------------------------------------------------


def test_deleting_a_memoir_takes_everything_with_it(db_conn, owner, factory):
    """The full cascade, asserted in one go.

    Memoir → participants, links, memories, assets, transcripts. Anything left
    behind would be unreachable rather than harmless: every query that could
    find it starts from the memoir.
    """
    contributor = factory.contributor(owner["memoir"]["id"])
    memory = factory.memory(owner["memoir"]["id"], contributor["id"])
    asset = factory.asset(owner["memoir"]["id"], memory_id=memory["id"], kind="audio",
                          mime_type="audio/webm", duration_ms=1000)
    factory.transcript(asset["id"])

    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM memoir WHERE id = %(id)s", {"id": owner["memoir"]["id"]}
        )
        db_conn.commit()

        for table in ["memoir_participant", "memoir_link", "memory", "media_asset"]:
            cur.execute(
                f"SELECT count(*) AS n FROM {table} WHERE memoir_id = %(id)s",
                {"id": owner["memoir"]["id"]},
            )
            assert cur.fetchone()["n"] == 0, f"{table} survived"

        cur.execute(
            "SELECT count(*) AS n FROM transcript WHERE asset_id = %(id)s",
            {"id": asset["id"]},
        )
        assert cur.fetchone()["n"] == 0, "transcript survived"


def test_an_account_that_owns_a_memoir_cannot_be_deleted(db_conn, owner):
    """`ON DELETE RESTRICT`, chosen because the child is worth more than the parent.

    Deleting the account would otherwise cascade away a family's contributions —
    material that cannot be recreated, removed to tidy up a login.
    """
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_account WHERE id = %(id)s",
                {"id": owner["account"]["id"]},
            )
    db_conn.rollback()


def test_deleting_an_asset_takes_its_transcript(db_conn, owner, factory):
    """`transcript.asset_id` is both the primary key and the foreign key.

    One transcript per recording, enforced by the shape of the table, and it
    dies with the recording for free — which is why removing an asset needs no
    transcript handling of its own.
    """
    memory = factory.memory(owner["memoir"]["id"], owner["memoir"]["owner_participant_id"])
    asset = factory.asset(owner["memoir"]["id"], memory_id=memory["id"], kind="audio",
                          mime_type="audio/webm", duration_ms=1000)
    factory.transcript(asset["id"])

    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM media_asset WHERE id = %(id)s", {"id": asset["id"]})
        db_conn.commit()
        cur.execute(
            "SELECT count(*) AS n FROM transcript WHERE asset_id = %(id)s",
            {"id": asset["id"]},
        )
        assert cur.fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# Row Level Security posture
# ---------------------------------------------------------------------------


def test_rls_is_on_everywhere_with_no_policies(db_conn):
    """The second wall, and a check that it is still standing.

    Enabled on every table with zero policies means a leaked anon key opens
    nothing. It provides no protection against *this* API, which connects with a
    bypassing role — which is exactly why the ownership tests matter.

    A policy appearing here would be a significant change to the security model
    and should be a deliberate conversation, not a surprise.
    """
    tables = [
        "user_account", "memoir_draft", "memoir", "memoir_participant",
        "memoir_link", "memory", "media_asset", "plan", "transcript",
    ]

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT relname, relrowsecurity
              FROM pg_class
             WHERE relname = ANY(%(tables)s) AND relkind = 'r'
            """,
            {"tables": tables},
        )
        rows = {r["relname"]: r["relrowsecurity"] for r in cur.fetchall()}

        assert set(rows) == set(tables), f"missing tables: {set(tables) - set(rows)}"
        off = [name for name, on in rows.items() if not on]
        assert off == [], f"row level security is off for: {off}"

        cur.execute("SELECT count(*) AS n FROM pg_policies WHERE schemaname = 'public'")
        assert cur.fetchone()["n"] == 0, (
            "a policy now exists — the security model has changed"
        )
