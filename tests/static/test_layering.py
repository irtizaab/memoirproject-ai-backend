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

def python_files(*subdirs: str) -> list[Path]:
    """Every source file. There is nothing under src/ these rules do not cover.

    There used to be an exclusion list here for the template scaffolding the
    project was generated from — `api/example.py` and friends, which broke two
    of the rules below and were held apart rather than allowed to weaken them.
    Those files are gone, so the honest version of this function is the one
    with no exceptions in it.
    """
    roots = [SRC / d for d in subdirs] if subdirs else [SRC]
    return sorted(p for root in roots for p in root.rglob("*.py"))


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
# Every route handler is `async def`, and nothing under src/ blocks the loop
# ---------------------------------------------------------------------------
#
# These two tests are a pair, and neither is much use alone.
#
# The driver stack is asynchronous: psycopg's AsyncConnection through the pool
# in integrations/db.py, and one shared httpx.AsyncClient in integrations/http.py.
# Given that, a handler written as a plain `def` is run in a threadpool, where
# it cannot await anything it needs — so the first test insists on `async def`.
#
# The second is the one that actually protects the process. A single
# synchronous driver call reintroduced anywhere under src/ — `psycopg.connect`,
# `httpx.get`, an `httpx.Client` — blocks the event loop for its whole
# duration, and with it every other request the worker is serving. That
# regression produces no error and no failing test of its own; the only symptom
# is a server that gets mysteriously slow under load. This is where it gets
# caught.


def test_route_handlers_are_async():
    """`async def`, not `def` — the decision this whole backend rests on.

    The database driver and the HTTP client are both asynchronous, so a
    handler has to be able to await them. A plain `def` handler cannot: FastAPI
    hands it to a threadpool with no running loop underneath it.
    """
    offenders = []
    for path in python_files("api"):
        relative = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            is_route = any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr in {"get", "post", "patch", "put", "delete"}
                for d in node.decorator_list
            )
            if is_route:
                offenders.append(f"{relative}:{node.lineno} {node.name}")

    assert offenders == [], (
        "route handlers that are not `async def`:\n" + "\n".join(offenders)
    )


# The synchronous driver calls that must never reappear under src/.
#
# `httpx.AsyncClient` is deliberately absent: constructing one is fine, and
# integrations/http.py does exactly that. It is the *synchronous* Client and
# the module-level shorthands that block.
BLOCKING_CALLS = {
    ("psycopg", "connect"),
    ("httpx", "Client"),
    ("httpx", "get"),
    ("httpx", "post"),
    ("httpx", "put"),
    ("httpx", "patch"),
    ("httpx", "head"),
    ("httpx", "delete"),
    ("httpx", "request"),
    ("httpx", "stream"),
}


def test_no_blocking_io_in_src():
    """No synchronous driver call anywhere the event loop can reach it.

    One of these is enough to stall every request in the worker for as long as
    it runs. The failure mode is latency under concurrency, not an exception,
    so nothing else in this suite would notice.

    The fix when this fails is never to make the caller a plain `def`: it is
    `async with db()` for the database, and `await (await client()).get(...)`
    for HTTP.
    """
    offenders = []
    for path in python_files():
        relative = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
                continue
            if (func.value.id, func.attr) in BLOCKING_CALLS:
                offenders.append(
                    f"{relative}:{node.lineno} {func.value.id}.{func.attr}()"
                )

    assert offenders == [], (
        "synchronous driver calls on the event loop:\n" + "\n".join(offenders)
    )
