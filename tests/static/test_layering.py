"""The house rules, enforced rather than described.

`AGENTS.md` states the layering rules and offers two grep commands to check
them. A rule nobody runs is a rule that quietly stops being true, so they run
here instead, on every `pytest`.

These need no database, no network and no application. They are the fastest
tests in the suite and the first ones to look at when something feels wrong
architecturally.
"""

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent.parent / "src"

# The leftover template scaffolding, excluded from every rule below.
#
# `AGENTS.md` says it plainly: "The `example_*` files alongside these are
# leftover template scaffolding, kept as a reference. They are not wired into
# the app." It also says that where they disagree with the real code, the real
# code wins — and they do disagree, on two counts. `api/example.py` is an
# `async def` handler and swallows every exception into a 500 carrying the raw
# error text. Both are exactly what the house rules forbid.
#
# So they are held apart rather than allowed to weaken the rules, and
# `test_example_scaffolding_is_not_wired_in` proves they stay unreachable. The
# real fix is deleting them; this set is what makes that a one-line change here.
EXAMPLE_SCAFFOLDING = {
    "api/example.py",
    "domain/example_feature/constants.py",
    "domain/example_feature/example_service.py",
    "domain/example_feature/utils.py",
    "models/example_models.py",
    "integrations/llm_client.py",
    "utils/string_helpers.py",
}


def python_files(*subdirs: str) -> list[Path]:
    """Every real source file. Template scaffolding is never included."""
    roots = [SRC / d for d in subdirs] if subdirs else [SRC]
    return sorted(
        p
        for root in roots
        for p in root.rglob("*.py")
        if p.relative_to(SRC).as_posix() not in EXAMPLE_SCAFFOLDING
    )


def code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines with comments and docstrings removed.

    The naive grep in `AGENTS.md` matches its own documentation — `api/
    memories.py` contains the word "Delete" in a docstring explaining the rule
    about deletes. Parsing the file and stripping string literals is what makes
    the check mean something.
    """
    source = path.read_text(encoding="utf-8")

    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - a syntax error fails elsewhere
        return []

    # Every line occupied by a string literal, which covers docstrings and any
    # multi-line SQL that is genuinely a string rather than executed code.
    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                doc_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    out = []
    for number, text in enumerate(source.splitlines(), start=1):
        if number in doc_lines:
            continue
        stripped = text.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append((number, text))
    return out


# ---------------------------------------------------------------------------
# api/ translates HTTP. It does not talk to the database.
# ---------------------------------------------------------------------------

SQL_VERB = re.compile(
    r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\b\s", re.IGNORECASE
)


def test_no_sql_in_the_api_layer():
    """The front desk does not query the database.

    Routes read a request, call the domain layer, and pick a status code. A
    query here means business logic has leaked upward, where it cannot be
    reused by the other caller that will eventually want it.
    """
    offenders = [
        f"{path.relative_to(SRC)}:{number}: {text.strip()}"
        for path in python_files("api")
        for number, text in code_lines(path)
        if SQL_VERB.search(text)
    ]
    assert offenders == [], "SQL found in src/api/:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# domain/ holds the rules. It has never heard of HTTP.
# ---------------------------------------------------------------------------


def test_domain_layer_does_not_import_fastapi():
    """Management does not know what a 404 is.

    The domain layer signals failure by returning None or raising its own
    exception; deciding what that means over HTTP belongs to the route. Keeping
    `fastapi` out is what makes those functions callable from a background task
    or a script without dragging a web framework in.
    """
    offenders = []
    for path in python_files("domain"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n.split(".")[0] == "fastapi" for n in names):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")

    assert offenders == [], "fastapi imported in src/domain/:\n" + "\n".join(offenders)


def test_integrations_layer_knows_nothing_about_memoirs():
    """The phone line has no opinion about hotels.

    `integrations/` wraps outside services. Feature knowledge here means the one
    layer meant to be reusable has been welded to this product.

    Checked on **identifiers**, not on string literals. The distinction is real:
    `memoir_id` as a parameter or `FROM memoir` in a query is coupling, while
    the string "X-Memoir-Webhook-Secret" in `assemblyai.py` is a header name —
    data being sent to a third party, which has to be called something. Matching
    the brand inside a string would flag naming, not architecture.
    """
    offenders = []
    for path in python_files("integrations"):
        relative = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            named = None
            if isinstance(node, ast.Name):
                named = node.id
            elif isinstance(node, ast.Attribute):
                named = node.attr
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                named = node.name
            elif isinstance(node, ast.arg):
                named = node.arg

            if named and re.search(r"memoir", named, re.IGNORECASE):
                offenders.append(f"{relative}:{node.lineno}: {named}")

        # A query against a product table would be a leak wherever it lived.
        for number, text in code_lines(path):
            if re.search(r"\b(FROM|INTO|UPDATE)\s+memoir", text, re.IGNORECASE):
                offenders.append(f"{relative}:{number}: {text.strip()}")

    assert offenders == [], (
        "src/integrations/ knows about memoirs:\n" + "\n".join(offenders)
    )


def test_example_scaffolding_is_not_wired_in():
    """The template leftovers stay unreachable.

    `api/example.py` is an anti-example — an `async def` handler that catches
    every exception and returns the raw error text as a 500. It is excluded from
    the house rules above on the grounds that nothing can reach it, so that
    exclusion is only honest while it stays true.

    Two things are asserted: `main.py` never registers the router, and the app
    really has no such path.
    """
    main = (SRC / "main.py").read_text(encoding="utf-8")
    assert "example" not in main, (
        "src/api/example.py has been wired into main.py — it breaks two house "
        "rules, so either fix it or take it back out"
    )

    from src.main import app

    # Read from the OpenAPI schema rather than `app.routes`: this FastAPI
    # version keeps included routers nested, so the flat list does not carry
    # their paths.
    assert "/example/greet" not in app.openapi()["paths"]


# ---------------------------------------------------------------------------
# Wiring stays in main.py
# ---------------------------------------------------------------------------


def test_main_defines_no_routes_of_its_own():
    """`main.py` is a switchboard. A route here belongs in `src/api/`."""
    source = (SRC / "main.py").read_text(encoding="utf-8")
    offenders = re.findall(r"@app\.(get|post|patch|put|delete)", source)
    assert offenders == [], f"routes defined in main.py: {offenders}"


# ---------------------------------------------------------------------------
# Configuration is read once, in one place
# ---------------------------------------------------------------------------

def test_environment_is_read_only_in_config():
    """One place knows what this app needs to run.

    Read the environment wherever it is convenient and six months later there
    are `os.getenv` calls in nine files, half with a different default, and no
    single place to look up what is required.
    """
    offenders = []
    for path in python_files():
        relative = path.relative_to(SRC).as_posix()
        if relative == "core/config.py":
            continue
        for number, text in code_lines(path):
            if re.search(r"os\.(environ|getenv)|load_dotenv", text):
                offenders.append(f"{relative}:{number}: {text.strip()}")

    assert offenders == [], (
        "environment read outside core/config.py:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# Failures are not swallowed
# ---------------------------------------------------------------------------

# `_over_budget` catches broadly on purpose and says why: if the budget query
# fails, transcribe anyway. Refusing to write out somebody's grandmother because
# a query misfired is the worse of the two errors, and the cost of being wrong
# is pennies. Listed so the rule holds everywhere else.
KNOWN_BROAD_CATCHES = {"domain/transcripts/transcript_service.py"}


def test_no_blanket_exception_handlers():
    """An unanticipated failure is a bug you want to see, not one to hide.

    There is no `except Exception: return 500` in this codebase. The central
    psycopg handler maps four specific SQLSTATE codes and re-raises everything
    else, deliberately, so a real fault arrives as a 500 with a traceback.
    """
    offenders = []
    for path in python_files():
        relative = path.relative_to(SRC).as_posix()
        if relative in KNOWN_BROAD_CATCHES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            caught = node.type
            bare = caught is None
            broad = isinstance(caught, ast.Name) and caught.id in {
                "Exception",
                "BaseException",
            }
            if bare or broad:
                offenders.append(f"{relative}:{node.lineno}")

    assert offenders == [], (
        "blanket exception handlers:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# Every route handler is a plain `def`
# ---------------------------------------------------------------------------

# `webhooks.post_assemblyai` is `async def` and awaits `request.json()`.
#
# Its own docstring claims there is no blocking database call on that path.
# There is: `apply_result` reaches `db()`, and can reach `fetch()`, both of
# which are synchronous. So this one handler does briefly block the event loop.
# Recorded here rather than quietly allowed — see the note in the test below.
KNOWN_ASYNC_HANDLERS = {("api/webhooks.py", "post_assemblyai")}


def test_route_handlers_are_sync_because_psycopg_is():
    """`def`, not `async def` — the decision this whole backend rests on.

    psycopg is synchronous. FastAPI runs an `async def` handler *on the event
    loop*, so a blocking query inside one freezes every other request in the
    process; a plain `def` handler is run in a threadpool instead, where
    blocking harms nobody.

    The wrong version is the one that looks more modern, it produces no error,
    and the only symptom is a server that gets mysteriously slow under load.
    That is exactly the kind of regression a test should catch.
    """
    offenders = []
    for path in python_files("api"):
        relative = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            is_route = any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr in {"get", "post", "patch", "put", "delete"}
                for d in node.decorator_list
            )
            if is_route and (relative, node.name) not in KNOWN_ASYNC_HANDLERS:
                offenders.append(f"{relative}:{node.lineno} {node.name}")

    assert offenders == [], (
        "async route handlers with a synchronous driver:\n" + "\n".join(offenders)
    )


@pytest.mark.xfail(
    reason=(
        "Known issue. post_assemblyai is `async def` but apply_result() reaches "
        "db() and httpx, both synchronous — so it blocks the event loop, which "
        "is the exact thing every other handler avoids. Low traffic, harmless "
        "today. The fix is making it a plain `def`; this xfail turns green and "
        "should then be deleted along with KNOWN_ASYNC_HANDLERS."
    ),
    strict=True,
)
def test_the_webhook_handler_should_also_be_sync():
    assert KNOWN_ASYNC_HANDLERS == set()
