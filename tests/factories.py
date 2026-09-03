"""Row builders.

Every fixture here writes SQL directly rather than calling the API.

That is the point. A test that sets up its world by calling `POST /memoirs/claim`
is really two tests joined together, and when claim breaks, every test in the
suite goes red at once and none of them says what is actually wrong. Building
rows underneath the application means a test about deletion fails only when
deletion is broken.

It also lets tests construct states the API deliberately cannot reach — a
published memoir, for instance. Nothing in this codebase ever sets
`status = 'published'`; there is no publish endpoint yet. The immutability rule
is enforced in six places and would otherwise be untestable.
"""

import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.conftest import DB_AVAILABLE, TEST_DATABASE_URL


class Factory:
    """Builds rows, and remembers ids so tests read as prose."""

    def __init__(self, conn):
        self.conn = conn

    def _one(self, sql: str, params: dict | None = None) -> dict:
        with self.conn.cursor() as cur:
            cur.execute(sql, params or {})
            return cur.fetchone()

    # --- people ----------------------------------------------------------

    def account(self, email: str | None = None, name: str = "Test Owner") -> dict:
        """An owner, and the `auth.users` row they hang off.

        Both, together, because `user_account.id` references `auth.users(id)`
        and Supabase would have created that row at signup. A factory that made
        only half of it would fail on a foreign key rather than on the thing
        under test.
        """
        user_id = str(uuid.uuid4())
        self._exec(
            "INSERT INTO auth.users (id) VALUES (%(id)s)", {"id": user_id}
        )
        return self._one(
            """
            INSERT INTO user_account (id, email, full_name)
            VALUES (%(id)s, %(email)s, %(name)s)
            RETURNING id, email, full_name, plan_code
            """,
            {
                "id": user_id,
                "email": email or f"owner-{user_id[:8]}@example.test",
                "name": name,
            },
        )

    def _exec(self, sql: str, params: dict | None = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql, params or {})

    # --- memoirs ---------------------------------------------------------

    def memoir(
        self,
        owner_id: str,
        *,
        subject_name: str = "Nusrat Bibi",
        status: str = "draft",
        never_forget: str | None = None,
        born_year: int | None = None,
        through_year: int | None = None,
    ) -> dict:
        """A memoir, its owner participant row, and a live contribute link.

        All three, because that is what `claim_draft` creates in one transaction
        and a memoir without an owner participant is a state the product treats
        as impossible.

        `published_at` is set in step with `status` — the
        `memoir_published_at_matches_status` CHECK is a biconditional, so a
        published memoir with a null timestamp is not storable.
        """
        memoir = self._one(
            """
            INSERT INTO memoir (created_by_user_id, subject_name, status,
                                published_at, never_forget, born_year,
                                through_year)
            VALUES (%(owner)s, %(subject)s, %(status)s::memoir_status,
                    CASE WHEN %(status)s = 'published' THEN now() END,
                    %(never_forget)s, %(born)s, %(through)s)
            RETURNING id, subject_name, status::text AS status
            """,
            {
                "owner": owner_id,
                "subject": subject_name,
                "status": status,
                "never_forget": never_forget,
                "born": born_year,
                "through": through_year,
            },
        )

        owner_participant = self._one(
            """
            INSERT INTO memoir_participant
                (memoir_id, role, user_id, display_name, relationship)
            VALUES (%(memoir)s, 'owner', %(user)s, 'Test Owner', 'other')
            RETURNING id
            """,
            {"memoir": memoir["id"], "user": owner_id},
        )

        link = self._one(
            """
            INSERT INTO memoir_link (memoir_id, scope)
            VALUES (%(memoir)s, 'contribute')
            RETURNING id, token
            """,
            {"memoir": memoir["id"]},
        )

        memoir["owner_participant_id"] = owner_participant["id"]
        memoir["link_token"] = link["token"]
        memoir["link_id"] = link["id"]
        return memoir

    def link(self, memoir_id: str, *, scope: str = "contribute") -> dict:
        """An extra link, for testing scope and revocation.

        `memoir_link_one_live_per_scope` permits one live link per scope, so a
        second `contribute` link needs the first revoked — which is what
        `revoke_link` is for.
        """
        return self._one(
            """
            INSERT INTO memoir_link (memoir_id, scope)
            VALUES (%(memoir)s, %(scope)s::link_scope)
            RETURNING id, token, scope::text AS scope
            """,
            {"memoir": memoir_id, "scope": scope},
        )

    def revoke_link(self, link_id: str) -> None:
        self._exec(
            "UPDATE memoir_link SET revoked_at = now() WHERE id = %(id)s",
            {"id": link_id},
        )

    def publish(self, memoir_id: str) -> None:
        """Flip a memoir to published.

        Only reachable from here — the application has no publish endpoint yet,
        and the CHECK constraint requires both columns to move together.
        """
        self._exec(
            """
            UPDATE memoir
               SET status = 'published', published_at = now()
             WHERE id = %(id)s
            """,
            {"id": memoir_id},
        )

    # --- contributors ----------------------------------------------------

    def contributor(
        self,
        memoir_id: str,
        *,
        display_name: str = "Ali",
        merged_into: str | None = None,
    ) -> dict:
        """A contributor participant, with a real 48-hex token.

        Generated by the same `gen_random_bytes(24)` the application uses, so a
        test asserting token shape is asserting the real thing.
        """
        return self._one(
            """
            INSERT INTO memoir_participant
                (memoir_id, role, display_name, relationship,
                 first_opened_at, contributor_token, merged_into)
            VALUES (%(memoir)s, 'contributor', %(name)s, 'other',
                    now(), encode(gen_random_bytes(24), 'hex'), %(merged)s)
            RETURNING id, display_name, contributor_token, merged_into
            """,
            {"memoir": memoir_id, "name": display_name, "merged": merged_into},
        )

    # --- memories and media ----------------------------------------------

    def memory(
        self,
        memoir_id: str,
        participant_id: str,
        *,
        kind: str = "text",
        title: str | None = "A memory",
        body_text: str | None = "The kitchen in August.",
        happened_on: str | None = None,
    ) -> dict:
        return self._one(
            """
            INSERT INTO memory (memoir_id, participant_id, kind, title,
                                body_text, happened_on)
            VALUES (%(memoir)s, %(participant)s, %(kind)s::memory_kind,
                    %(title)s, %(body)s, %(happened)s)
            RETURNING id, kind::text AS kind, title, body_text
            """,
            {
                "memoir": memoir_id,
                "participant": participant_id,
                "kind": kind,
                "title": title,
                "body": body_text,
                "happened": happened_on,
            },
        )

    def asset(
        self,
        memoir_id: str,
        *,
        memory_id: str | None = None,
        kind: str = "image",
        mime_type: str = "image/jpeg",
        uploaded: bool = True,
        byte_size: int = 1024,
        duration_ms: int | None = None,
    ) -> dict:
        """A media asset.

        `uploaded=False` produces a reservation — a row with a null
        `uploaded_at`, which is what an abandoned upload leaves behind and which
        several rules treat differently from a real file.
        """
        return self._one(
            """
            INSERT INTO media_asset
                (memoir_id, memory_id, kind, storage_path, mime_type,
                 byte_size, duration_ms, uploaded_at)
            VALUES (%(memoir)s, %(memory)s, %(kind)s::asset_kind,
                    %(path)s, %(mime)s, %(size)s, %(duration)s,
                    CASE WHEN %(uploaded)s THEN now() END)
            RETURNING id, kind::text AS kind, storage_path, byte_size
            """,
            {
                "memoir": memoir_id,
                "memory": memory_id,
                "kind": kind,
                "path": f"{memoir_id}/{uuid.uuid4().hex}.jpg",
                "mime": mime_type,
                "size": byte_size,
                "duration": duration_ms,
                "uploaded": uploaded,
            },
        )

    def transcript(
        self,
        asset_id: str,
        *,
        status: str = "done",
        text: str | None = "What was said.",
        provider_id: str | None = None,
        error: str | None = None,
    ) -> dict:
        return self._one(
            """
            INSERT INTO transcript (asset_id, status, text, provider_id, error)
            VALUES (%(asset)s, %(status)s::transcript_status, %(text)s,
                    %(provider)s, %(error)s)
            RETURNING asset_id, status::text AS status, provider_id
            """,
            {
                "asset": asset_id,
                "status": status,
                "text": text,
                "provider": provider_id or f"job-{uuid.uuid4().hex[:12]}",
                "error": error,
            },
        )

    # --- chapters --------------------------------------------------------
    #
    # Nothing in the application writes any of these. Assembly is a later
    # slice, so the read path is tested against rows built here — which is the
    # same reason `publish()` exists above.

    def chapter(
        self,
        memoir_id: str,
        *,
        ordinal: int = 0,
        title: str = "The House on Ellsworth Lane",
        from_year: int | None = 1928,
        through_year: int | None = 1934,
    ) -> dict:
        return self._one(
            """
            INSERT INTO chapter (memoir_id, ordinal, title, from_year,
                                 through_year)
            VALUES (%(memoir)s, %(ordinal)s, %(title)s, %(from_year)s,
                    %(through_year)s)
            RETURNING id, ordinal, title
            """,
            {
                "memoir": memoir_id,
                "ordinal": ordinal,
                "title": title,
                "from_year": from_year,
                "through_year": through_year,
            },
        )

    def block(
        self,
        memoir_id: str,
        chapter_id: str,
        *,
        ordinal: int = 0,
        kind: str = "paragraph",
        text: str = "Eleanor was born on the fifteenth of March, 1928.",
    ) -> dict:
        """A paragraph or a pulled line."""
        return self._one(
            """
            INSERT INTO chapter_block (memoir_id, chapter_id, ordinal, kind,
                                       text)
            VALUES (%(memoir)s, %(chapter)s, %(ordinal)s, %(kind)s::block_kind,
                    %(text)s)
            RETURNING id, ordinal, kind::text AS kind, text
            """,
            {
                "memoir": memoir_id,
                "chapter": chapter_id,
                "ordinal": ordinal,
                "kind": kind,
                "text": text,
            },
        )

    def figure(
        self,
        memoir_id: str,
        chapter_id: str,
        *,
        asset_id: str,
        anchor_block_id: str,
        ordinal: int = 1,
        placement: str = "margin",
    ) -> dict:
        """A photograph on the page, anchored to the paragraph it belongs to."""
        return self._one(
            """
            INSERT INTO chapter_block (memoir_id, chapter_id, ordinal, kind,
                                       asset_id, placement, anchor_block_id)
            VALUES (%(memoir)s, %(chapter)s, %(ordinal)s, 'figure',
                    %(asset)s, %(placement)s::figure_placement, %(anchor)s)
            RETURNING id, ordinal, kind::text AS kind, placement::text AS placement
            """,
            {
                "memoir": memoir_id,
                "chapter": chapter_id,
                "ordinal": ordinal,
                "asset": asset_id,
                "placement": placement,
                "anchor": anchor_block_id,
            },
        )

    def source(
        self,
        memoir_id: str,
        block_id: str,
        *,
        memory_id: str,
        participant_id: str,
        start_offset: int | None = None,
        end_offset: int | None = None,
        diverges: bool = False,
    ) -> dict:
        """Which human this passage came from, and which words are theirs."""
        return self._one(
            """
            INSERT INTO block_source (memoir_id, block_id, memory_id,
                                      participant_id, start_offset, end_offset,
                                      diverges)
            VALUES (%(memoir)s, %(block)s, %(memory)s, %(participant)s,
                    %(start)s, %(end)s, %(diverges)s)
            RETURNING id, start_offset, end_offset, diverges
            """,
            {
                "memoir": memoir_id,
                "block": block_id,
                "memory": memory_id,
                "participant": participant_id,
                "start": start_offset,
                "end": end_offset,
                "diverges": diverges,
            },
        )

    def thread(
        self,
        memoir_id: str,
        chapter_id: str,
        block_id: str,
        *,
        start_offset: int | None = None,
        end_offset: int | None = None,
    ) -> dict:
        return self._one(
            """
            INSERT INTO comment_thread (memoir_id, chapter_id, block_id,
                                        start_offset, end_offset)
            VALUES (%(memoir)s, %(chapter)s, %(block)s, %(start)s, %(end)s)
            RETURNING id, start_offset, end_offset
            """,
            {
                "memoir": memoir_id,
                "chapter": chapter_id,
                "block": block_id,
                "start": start_offset,
                "end": end_offset,
            },
        )

    def comment(
        self,
        memoir_id: str,
        thread_id: str,
        participant_id: str,
        *,
        body: str = "My mother told this one differently.",
    ) -> dict:
        return self._one(
            """
            INSERT INTO comment (memoir_id, thread_id, participant_id, body)
            VALUES (%(memoir)s, %(thread)s, %(participant)s, %(body)s)
            RETURNING id, body
            """,
            {
                "memoir": memoir_id,
                "thread": thread_id,
                "participant": participant_id,
                "body": body,
            },
        )

    # --- drafts ----------------------------------------------------------

    def draft(self, *, subject_name: str | None = "Nusrat Bibi") -> dict:
        return self._one(
            """
            INSERT INTO memoir_draft (subject_name)
            VALUES (%(subject)s)
            RETURNING id, token, subject_name
            """,
            {"subject": subject_name},
        )


@pytest.fixture
def factory():
    """Row builders on their own autocommit connection.

    Autocommit so rows are visible to the application immediately — the app
    opens its own connections, and anything left uncommitted here would be
    invisible to the request under test.
    """
    if not DB_AVAILABLE:
        pytest.skip("no test database")

    with psycopg.connect(
        TEST_DATABASE_URL, row_factory=dict_row, autocommit=True
    ) as conn:
        yield Factory(conn)


@pytest.fixture
def owner(factory):
    """The common case: an account, a memoir, an owner participant, a link."""
    account = factory.account()
    memoir = factory.memoir(account["id"])
    return {"account": account, "memoir": memoir}


@pytest.fixture
def stranger(factory):
    """A second account with its own memoir.

    Present in most security tests, because "not yours" needs somebody else to
    be the owner of the thing being reached for.
    """
    account = factory.account(name="Someone Else")
    memoir = factory.memoir(account["id"], subject_name="A Different Subject")
    return {"account": account, "memoir": memoir}
