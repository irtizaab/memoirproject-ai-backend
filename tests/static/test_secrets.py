"""Where secrets are allowed to be, and where 403 is allowed to be.

Both are structural promises: they are true of the whole source tree or they are
not true at all, so they are checked by reading the tree rather than by
exercising a request.
"""

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent.parent / "src"


def python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


# ---------------------------------------------------------------------------
# The one real secret
# ---------------------------------------------------------------------------

# The service role key bypasses Row Level Security and can read or write
# anything in the Supabase project. It exists for exactly one reason: signing
# upload URLs for contributors, who have no credential of their own.
#
# `config.py` declares it. `supabase_storage.py` uses it. Nowhere else may even
# name it — a third file appearing in this set is a review conversation, not a
# passing test.
SERVICE_KEY_ALLOWED = {
    "core/config.py",
    "integrations/supabase_storage.py",
}


@pytest.mark.security
def test_service_role_key_is_confined_to_one_module():
    """The key that can read every memoir in the project.

    Confinement is the whole mitigation. It is never passed as an argument,
    never returned, and never logged — so the blast radius of a mistake is one
    file that a reviewer can read in full.
    """
    mentions = set()
    for path in python_files():
        text = path.read_text(encoding="utf-8")
        if "supabase_service_role_key" in text:
            mentions.add(path.relative_to(SRC).as_posix())

    unexpected = mentions - SERVICE_KEY_ALLOWED
    assert unexpected == set(), (
        f"supabase_service_role_key referenced outside its module: {unexpected}"
    )


@pytest.mark.security
def test_no_secret_is_ever_logged():
    """Log an identifier, never a credential.

    Checked by looking at what each `logger.*` call interpolates. The codebase
    already gets this right — it logs "Rejected an AssemblyAI callback with a
    bad secret" without the secret, and "Contributor token did not match"
    without the token. This keeps it that way.
    """
    secret_names = re.compile(
        r"\b("
        r"password|secret|service_role_key|anon_key|api_key"
        r"|access_token|contributor_token|participant_token|draft_token"
        r")\b",
        re.IGNORECASE,
    )

    offenders = []
    for path in python_files():
        relative = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "logger"
            ):
                continue

            # Every argument after the format string, plus anything spliced into
            # an f-string used as the message.
            for arg in node.args[1:]:
                if secret_names.search(ast.unparse(arg)):
                    offenders.append(f"{relative}:{node.lineno}: {ast.unparse(arg)}")
            if node.args and isinstance(node.args[0], ast.JoinedStr):
                rendered = ast.unparse(node.args[0])
                if secret_names.search(rendered):
                    offenders.append(f"{relative}:{node.lineno}: {rendered}")

    assert offenders == [], "credentials reaching the log:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# 404, never 403
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_the_api_never_answers_403():
    """A memoir that is not yours does not exist, as far as you are concerned.

    403 means "I know who you are and you still may not" — which confirms the id
    you guessed belongs to a real memoir. 404 says nothing at all. It costs
    nothing to avoid, so the whole codebase is built for it: the ownership
    helpers return None rather than raising, the domain layer passes that
    upward, and the route turns it into 404.

    A single 403 anywhere would open the oracle back up, so this reads the
    source rather than trusting a route to be tested individually.
    """
    offenders = []
    for path in python_files():
        relative = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name != "HTTPException":
                continue
            for keyword in node.keywords:
                if keyword.arg == "status_code":
                    value = getattr(keyword.value, "value", None)
                    if value == 403:
                        offenders.append(f"{relative}:{node.lineno}")

    assert offenders == [], (
        "403 leaks the existence of somebody else's memoir:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# SQL is parameterised
# ---------------------------------------------------------------------------

# Two places build a SET clause by interpolating column names, and both are
# safe — but for different reasons, and only one of them is safe by
# construction:
#
#   memory_service.update_memory  filters names through a hard-coded
#                                 {"title","body_text","happened_on"} set, so
#                                 nothing else can reach the f-string.
#
#   draft_service.update_draft    relies entirely on Pydantic having dropped
#                                 undeclared keys. Weaker: adding
#                                 extra="allow" to DraftUpdate would open it.
#                                 `test_draft_update_rejects_unknown_fields`
#                                 in the security tier guards that.
#
# Values always travel as %(name)s in both.
SQL_INTERPOLATION_ALLOWED = {
    "domain/memories/memory_service.py",
    "domain/drafts/draft_service.py",
}


@pytest.mark.security
def test_no_value_is_ever_interpolated_into_sql():
    """Values go through placeholders, always.

    Glue a value into query text and you have SQL injection — the database
    cannot tell the difference between the instruction you wrote and the
    fragment somebody typed. `%(name)s` keeps them strictly apart.

    This looks for f-strings containing a SQL verb, which is the shape the
    mistake takes.
    """
    verb = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", re.IGNORECASE)

    offenders = []
    for path in python_files():
        relative = path.relative_to(SRC).as_posix()
        if relative in SQL_INTERPOLATION_ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr) and verb.search(ast.unparse(node)):
                offenders.append(f"{relative}:{node.lineno}")

    assert offenders == [], (
        "SQL built by interpolation:\n" + "\n".join(offenders)
    )


@pytest.mark.security
def test_update_memory_has_a_hard_coded_column_allow_list():
    """The dynamic SET clause is bounded by a literal, not by trust.

    `update_memory` builds `title = %(title)s, ...` from whichever keys arrived.
    What makes that safe is the line above it: keys are filtered through a set
    literal, so a key like `title=1; DROP TABLE memory --` never reaches the
    f-string, `columns` comes back empty, and the function returns None.

    Asserted on the source because it is a property of how the code is written,
    and a future refactor that reads the allow-list from somewhere else would
    still pass a behavioural test while being much easier to get wrong.
    """
    source = (SRC / "domain" / "memories" / "memory_service.py").read_text(
        encoding="utf-8"
    )
    assert 'allowed = {"title", "body_text", "happened_on"}' in source, (
        "update_memory's column allow-list is no longer a literal set — "
        "check that client input still cannot reach the SET clause"
    )
