"""The test harness.

Read this before writing a test — most of what looks odd here is working around
something specific, and the comments say what.

---------------------------------------------------------------------------
The environment block below runs BEFORE any `src.*` import, on purpose
---------------------------------------------------------------------------
Three things in this codebase are decided at import time, not at call time, so
a fixture is already too late for all of them:

  1. `settings = Settings()` at the bottom of `src/core/config.py`. It runs the
     moment the module is first imported, and `database_url` and `supabase_url`
     have no defaults — a missing one is a crash at import, which is deliberate
     in production and inconvenient here.

  2. `PyJWKClient(settings.supabase_jwks_url, ...)` at module scope in
     `src/integrations/supabase_auth.py`. The JWKS URL is frozen into that
     object at import, so repointing `settings.supabase_url` afterwards does
     nothing. The auth fixtures patch `_jwk_client` itself instead.

  3. `if settings.enable_dev_routes:` in `src/main.py`, which decides whether
     `/dev/signin` exists at all.

pytest imports this file before collecting anything in `tests/`, so setting the
environment at the top of the module is the only place that is early enough.
"""

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- the environment, before anything else -------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

# A fake Supabase project. Every value is syntactically valid and points
# nowhere, so a test that accidentally makes a real call fails loudly instead of
# quietly succeeding against something real.
os.environ["SUPABASE_URL"] = "https://project.supabase.test"
os.environ["SUPABASE_ANON_KEY"] = "test-anon-key"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-service-role-key"
os.environ["SUPABASE_STORAGE_BUCKET"] = "memoir-media-test"

# A known secret, so the webhook tests can send the right one and the wrong one.
os.environ["ASSEMBLYAI_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["ASSEMBLYAI_API_KEY"] = "test-assemblyai-key"

# Empty, as it is on a laptop: no public URL means no webhook is ever requested
# and the reconcile-on-read path carries transcription by itself.
os.environ["PUBLIC_BASE_URL"] = ""

# OFF, matching production. This is a security posture, not a convenience: with
# it off the dev router is never imported, and `test_dev_routes_are_absent`
# proves `/dev/signin` cannot be reached by guessing the path.
os.environ["ENABLE_DEV_ROUTES"] = "false"

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/memoir_test",
)

# ---------------------------------------------------------------------------
# The guard that stops this suite from ever touching real memoirs.
#
# The suite TRUNCATEs every table between tests. Pointed at the wrong database
# that is not a failing test, it is a family's recordings gone. So the database
# must be named `memoir_test...` and nothing else will run.
# ---------------------------------------------------------------------------
_DB_NAME = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
if not _DB_NAME.startswith("memoir_test"):
    raise RuntimeError(
        f"TEST_DATABASE_URL points at a database named {_DB_NAME!r}. "
        "The test suite truncates every table it can reach, so it refuses to "
        "run against anything not named memoir_test*."
    )

# `src.core.config` reads DATABASE_URL at import. Point it at the test database
# so the app under test can never open a connection to anything else.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

# --- now it is safe to import ------------------------------------------------

import jwt  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from src.api.dependencies import CurrentUser, current_user  # noqa: E402
from src.api.media import optional_user_id  # noqa: E402
from src.main import app  # noqa: E402

pytest_plugins = ["tests.factories"]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

# Everything except `plan`. Plan rows are seed data inserted by migrations 0004
# and 0005, and the billing tests read them — truncating them would leave a
# database that no longer matches the one the migrations describe.
MUTABLE_TABLES = [
    "comment",
    "comment_thread",
    "block_source",
    "chapter_block",
    "chapter",
    "transcript",
    "media_asset",
    "memory",
    "memoir_link",
    "memoir_participant",
    "memoir",
    "memoir_draft",
    "user_account",
]


def _database_is_reachable() -> bool:
    """Whether a Postgres is listening, without raising if it is not."""
    admin = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    try:
        with psycopg.connect(admin, connect_timeout=3):
            return True
    except Exception:
        return False


DB_AVAILABLE = _database_is_reachable()

# Every db-marked test skips with this one sentence rather than erroring, so the
# tiers that need no database stay useful before Postgres is installed.
requires_db = pytest.mark.skipif(
    not DB_AVAILABLE,
    reason=(
        "No test database. Start one with:\n"
        "  docker run --name memoir-pg -e POSTGRES_PASSWORD=postgres "
        "-p 5433:5432 -d postgres:17"
    ),
)


@pytest.fixture(scope="session", autouse=True)
def _build_test_database():
    """Drop, recreate and migrate the test database once per session.

    Rebuilt from scratch rather than migrated in place, so the schema under test
    is always exactly what a fresh `migrations/` produces. A test suite running
    against a database that drifted from the migrations proves nothing about
    what a real deployment would do.
    """
    if not DB_AVAILABLE:
        yield
        return

    admin_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"

    with psycopg.connect(admin_url, autocommit=True) as conn, conn.cursor() as cur:
        # Terminate anything still connected, or DROP DATABASE blocks forever.
        cur.execute(
            """
            SELECT pg_terminate_backend(pid)
              FROM pg_stat_activity
             WHERE datname = %(name)s AND pid <> pg_backend_pid()
            """,
            {"name": _DB_NAME},
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{_DB_NAME}"')

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            # The one thing a bare Postgres cannot provide.
            #
            # `user_account.id REFERENCES auth.users(id)` — that is Supabase's
            # own table, in Supabase's own schema. Locally it does not exist, so
            # migration 0001 fails on its first CREATE TABLE without this.
            #
            # A stand-in rather than a copy: only the column the foreign key
            # points at matters, and pretending to reproduce Supabase's real
            # auth schema would be a lie that drifts.
            cur.execute("CREATE SCHEMA IF NOT EXISTS auth")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS auth.users (
                    id uuid PRIMARY KEY
                )
                """
            )

        # In filename order, which is chronological — the four-digit prefixes
        # are zero-padded precisely so that sorting is the correct order.
        for path in sorted((REPO_ROOT / "migrations").glob("*.sql")):
            with conn.cursor() as cur:
                cur.execute(path.read_text(encoding="utf-8"))

    yield


@pytest.fixture(autouse=True)
def _clean_tables():
    """Empty every mutable table before each test.

    Not a shared transaction rolled back at the end, which would be faster: the
    app opens its own connection per call through `db()`, so a test cannot
    enclose the application's work inside a transaction it controls. Truncation
    is the honest option at this size.
    """
    if not DB_AVAILABLE:
        yield
        return

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"TRUNCATE {', '.join(MUTABLE_TABLES)} RESTART IDENTITY CASCADE"
            )
    yield


@pytest.fixture
def db_conn():
    """A direct connection, for tests that assert on rows rather than responses.

    `dict_row` to match what the application sees through `integrations/db.py`.
    """
    with psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row) as conn:
        yield conn


# ---------------------------------------------------------------------------
# Real signing keys — deliberately not a mock
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def signing_key():
    """An ES256 keypair standing in for Supabase's.

    Real keys, real signatures. The whole value of the auth tests is that they
    exercise `verify_access_token` exactly as written — the algorithm
    allow-list, the audience and issuer checks, the required claims and the
    thirty seconds of leeway. Patching `verify_access_token` to return a dict
    would test nothing except that the patch works.

    ES256 because that is what the Supabase project signs with.
    """
    private = ec.generate_private_key(ec.SECP256R1())
    return {
        "private": private,
        "public": private.public_key(),
        "kid": "test-key-1",
    }


@pytest.fixture(autouse=True)
def _patch_jwks(signing_key, monkeypatch):
    """Serve our public key where the JWKS client would fetch Supabase's.

    `_jwk_client` is built at module import, so its URL is already frozen and
    changing settings would do nothing. Replacing the object is the seam.

    Autouse so no test can accidentally reach the network for a key.
    """

    class _Key:
        key = signing_key["public"]

    class _FakeJWKClient:
        def get_signing_key_from_jwt(self, token):
            header = jwt.get_unverified_header(token)
            if header.get("kid") != signing_key["kid"]:
                # The real client raises this for a key it cannot resolve, and
                # `verify_access_token` turns it into a 401. Matching the real
                # failure mode is what makes the unknown-kid test meaningful.
                raise jwt.PyJWKClientError("no key for kid")
            return _Key()

    monkeypatch.setattr(
        "src.integrations.supabase_auth._jwk_client", _FakeJWKClient()
    )


@pytest.fixture
def make_token(signing_key):
    """Mint an access token. Every claim overridable, so tests can break one.

    Defaults are a valid Supabase token for this project. A test that wants to
    prove a wrong issuer is refused passes `iss=...` and changes nothing else,
    which keeps the test about the one thing it is about.
    """
    from src.core.config import settings

    def _make(
        sub: str | None = None,
        *,
        email: str = "owner@example.test",
        full_name: str = "Test Owner",
        aud: str | None = "authenticated",
        iss: str | None = None,
        exp_delta: timedelta = timedelta(hours=1),
        algorithm: str = "ES256",
        key=None,
        include_exp: bool = True,
        kid: str | None = None,
        extra_claims: dict | None = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        claims: dict = {
            "sub": sub or str(uuid.uuid4()),
            "email": email,
            "user_metadata": {"full_name": full_name},
            "iat": now,
        }
        if include_exp:
            claims["exp"] = now + exp_delta
        if aud is not None:
            claims["aud"] = aud
        claims["iss"] = iss if iss is not None else settings.supabase_issuer
        if extra_claims:
            claims.update(extra_claims)

        return jwt.encode(
            claims,
            key if key is not None else signing_key["private"],
            algorithm=algorithm,
            headers={"kid": kid or signing_key["kid"]},
        )

    return _make


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """A TestClient with no credential of any kind.

    Used by everything that is about authentication, and by the public routes.
    `lifespan` only logs, so constructing this needs no services.
    """
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def as_owner(client):
    """Sign requests as a given user, bypassing token verification.

    `current_user` is a FastAPI dependency, so `dependency_overrides` replaces
    it cleanly. Tests that are about *authorization* — whose memoir is this —
    should not also be paying to verify a signature; tests that are about
    *authentication* use `make_token` and the real path instead.

    Returns the client, so a test reads `as_owner(user_id).get(...)`.
    """

    def _as(user_id: str, email: str = "owner@example.test", name: str = "Owner"):
        app.dependency_overrides[current_user] = lambda: CurrentUser(
            id=user_id, email=email, full_name=name
        )
        # The upload routes do not depend on `current_user` — they take either
        # credential, so they use `optional_user_id`, which returns None instead
        # of raising. Overriding only `current_user` would leave an "owner"
        # client unauthenticated on /media, which is not what the name promises.
        app.dependency_overrides[optional_user_id] = lambda: user_id
        return client

    yield _as
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Fake integrations
# ---------------------------------------------------------------------------


class FakeStorage:
    """An in-memory object store.

    Enough for the three-step upload to run end to end with no bucket and no
    network: reserve, PUT, confirm. `objects` maps a storage path to bytes, so a
    test can assert a file was really deleted rather than that a function was
    called.
    """

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.signed_uploads: list[str] = []
        self.deleted: list[str] = []

    def create_signed_upload_url(self, path: str) -> str:
        self.signed_uploads.append(path)
        return f"https://storage.test/upload/{path}"

    def create_signed_download_url(self, path: str) -> str:
        return f"https://storage.test/read/{path}"

    def create_signed_download_urls(self, paths: list[str]) -> dict[str, str]:
        return {p: f"https://storage.test/read/{p}" for p in paths}

    def object_size(self, path: str) -> int:
        from src.integrations.supabase_storage import StorageError

        if path not in self.objects:
            # What the real wrapper does when storage has nothing at the path,
            # which the confirm route turns into a 409.
            raise StorageError(f"no object at {path}")
        return len(self.objects[path])

    def delete_object(self, path: str) -> None:
        self.deleted.append(path)
        self.objects.pop(path, None)

    def put(self, path: str, content: bytes = b"test-bytes") -> None:
        """Stand in for the browser's direct PUT to storage."""
        self.objects[path] = content


@pytest.fixture
def storage(monkeypatch):
    """Replace storage everywhere it is imported.

    Patched per importing module, not on the integrations module alone: each
    domain module did `from src.integrations.supabase_storage import ...`, which
    binds the name into its own namespace at import. Patching only the source
    would leave those bindings pointing at the real functions.
    """
    fake = FakeStorage()

    targets = [
        "src.domain.chapters.chapter_service",
        "src.domain.media.media_service",
        "src.domain.memories.memory_service",
        "src.domain.transcripts.transcript_service",
    ]
    names = {
        "create_signed_upload_url": fake.create_signed_upload_url,
        "create_signed_download_url": fake.create_signed_download_url,
        "create_signed_download_urls": fake.create_signed_download_urls,
        "object_size": fake.object_size,
        "delete_object": fake.delete_object,
    }

    for module in targets:
        for name, impl in names.items():
            monkeypatch.setattr(f"{module}.{name}", impl, raising=False)

    return fake


@pytest.fixture(autouse=True)
def _no_transcription(monkeypatch):
    """Stop every test from submitting audio to a transcription provider.

    Autouse and deliberately blunt. A test that wants the transcription path
    asks for it explicitly by patching further; nothing should reach AssemblyAI
    by simply uploading a recording, which is what `POST /media/uploads/{id}/
    complete` does in production.
    """
    monkeypatch.setattr(
        "src.api.media.request_transcription", lambda *a, **k: None, raising=False
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

# 24 random bytes rendered as hex, which is what every token in this product is.
TOKEN_PATTERN = re.compile(r"^[0-9a-f]{48}$")


def find_key(payload, key: str) -> bool:
    """Whether `key` appears anywhere in a nested response body.

    The leakage tests need this: asserting a top-level key is absent proves very
    little when the thing you are worried about could surface three levels down
    inside an asset or a transcript.
    """
    if isinstance(payload, dict):
        return key in payload or any(find_key(v, key) for v in payload.values())
    if isinstance(payload, list):
        return any(find_key(item, key) for item in payload)
    return False


def find_value(payload, needle: str) -> bool:
    """Whether `needle` appears as any string value, at any depth."""
    if isinstance(payload, dict):
        return any(find_value(v, needle) for v in payload.values())
    if isinstance(payload, list):
        return any(find_value(item, needle) for item in payload)
    return isinstance(payload, str) and needle in payload
