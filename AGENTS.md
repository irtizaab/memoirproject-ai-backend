# The Memoir Project — backend

FastAPI + Postgres (Supabase). The API this serves is the backend for the onboarding flow
prototyped in `memoir-onboarding_7.html`.

---

## Instructions

# The Memoir Project — API

## What this product is

A web platform where a family creates one memoir for a person they've lost (or
want to record while still living), shares a single link, and everyone who
knew that subject contributes memories — by voice, text, or photograph — from
their own phone, with no account required. The system organizes contributions
into a chaptered, searchable archive. The finished memoir publishes as a
private webpage (searchable, commentable, permanent) and a downloadable PDF.

The founder (the person you're working with) has no prior backend experience
and is learning by building this. See "How to work with the maintainer" below
— it changes how you should behave, not just what you should build.

## Non-negotiable product constraints

These come from the PRD and shape every technical decision. Violating one is
a product bug, not a style preference:

- **Contributors never create accounts.** Access is by unguessable link token
  only. Any feature that assumes a contributor has a `user_id` is wrong.
- **Publication is immutable.** Once a memoir's status flips to `published`,
  its content can never be edited — not by the owner, not by an admin. New
  material after publication means a new memoir, not an edit.
- **Never fabricate.** The system organizes and connects what humans
  supplied. It does not invent memories, generate synthetic media, or merge
  two people's conflicting accounts into one "true" version. Divergent
  accounts of the same event are both kept and shown side by side.
- **Media never lives in the database as a blob.** Audio, video, and photos
  go to object storage via presigned URLs. The database stores metadata only.
- **No gamification.** No streaks, badges, completion percentages, or
  re-engagement nudges anywhere, including in this API's responses or error
  copy.

## Architecture

Two separate repos, not a monorepo:

- `memoirproject-frontend` — Next.js. Talks to Supabase Auth directly for
  login/signup. Talks to this API for everything else. Never queries Postgres
  directly.
- `memoirproject-api` (this repo) — FastAPI. Owns the database: all
  migrations live here. Holds the Supabase **service role** key and connects
  to Postgres directly via `psycopg`. Verifies Supabase JWTs to identify
  callers; never sees a password.

The database is Postgres, hosted on Supabase. Row Level Security is enabled
on every table with **zero policies** — this API is what authorizes access,
not RLS. RLS is a second wall in case a privileged key ever leaks into a
client, not the primary defense.

## Current state (update this section as you build)

Built and working — slice 1 is complete end to end:

- `GET /health` — confirms the API can reach Postgres
- `POST /drafts`, `PATCH /drafts/{draft_id}` — the pre-signup onboarding
  answers, authorized by a token header (`X-Draft-Token`), not a login
- **Supabase JWT verification.** The project signs with **ES256** and publishes
  a JWKS, so the API verifies against Supabase's *public* key and holds no
  secret capable of forging a token. Keys are cached in-process for 5 minutes,
  so this is not a network call per request.
- `POST /memoirs/claim` — the atomic transaction that turns a draft plus an
  authenticated user into `user_account` + `memoir` + owner `memoir_participant`
  + `memoir_link`. Requires **both** a bearer token (who you are) and
  `X-Draft-Token` (which browser session started this draft).
- `GET /me` — the caller's identity plus the memoirs they own, for the
  dashboard. Returns 200 with an empty list for a user who has signed up but
  claimed nothing; that is a normal state, not a 404.
- `GET /j/{token}` — link resolution for contributors. The **only**
  unauthenticated route, by design. Bumps `open_count`, honours `revoked_at`,
  and is filtered by a `response_model` so the owner's private `never_forget`
  cannot leak.
- `POST /dev/signin` — dev-only token minting, registered **only** when
  `ENABLE_DEV_ROUTES=true`. The import itself sits inside the `if`, so the
  module is never loaded in production.
- A central `psycopg.Error` handler mapping SQLSTATE codes to clean 4xx JSON
  responses instead of raw 500s, in `src/core/error_handlers.py`

File layout:

```
src/
  main.py                            wiring only: app, CORS, error handlers, routers
  core/
    config.py                        settings.* — the only place env is read
    error_handlers.py                psycopg.Error -> 400/409
    app_lifespan.py                  startup/shutdown hook
    logging_config.py                setup_logging()
  integrations/
    db.py                            db() connection, ping()
    supabase_auth.py                 verify_access_token(), password_signin()
  models/
    draft_models.py                  DraftUpdate
    memoir_models.py                 ClaimRequest, MemoirSummary, AccountOverview,
                                     LinkInvitation
  domain/
    drafts/draft_service.py          create_draft(), update_draft()
    memoirs/memoir_service.py        claim_draft(), list_memoirs_for_owner()
    links/link_service.py            resolve_link()
  api/
    dependencies.py                  current_user -> CurrentUser (401s live here)
    health.py                        GET  /health
    drafts.py                        POST /drafts, PATCH /drafts/{draft_id}
    memoirs.py                       POST /memoirs/claim          (auth)
    accounts.py                      GET  /me                     (auth)
    links.py                         GET  /j/{token}              (public)
    dev.py                           POST /dev/signin             (gated)
```

The `example_*` files alongside these are leftover template scaffolding, kept
as a reference. They are not wired into the app.

Environment variables (`.env`): `DATABASE_URL`, `SUPABASE_URL`,
`SUPABASE_ANON_KEY`, `ENABLE_DEV_ROUTES`. Neither Supabase value is a secret —
both ship in every frontend bundle. The API deliberately does **not** hold the
service role key for auth; it only verifies.

Not built yet:
- Everything past slice 1: memories, media, chapters, comments, billing.
  Migration `0001` has no tables for any of it, on purpose.
- Google OAuth. The frontend's button calls `signInWithOAuth` and is wired to
  redirect back to `/onboarding`, but only the email provider is enabled on the
  Supabase project, so it currently answers "provider is not enabled". Nothing
  in this API needs changing when it is switched on — a Google-issued JWT
  verifies identically.
- The contribute flow. `GET /j/{token}` resolves and the frontend renders an
  invitation page, but there is nowhere to submit a memory yet.
- Link revocation and re-issue. `memoir_link.revoked_at` is honoured
  everywhere but no endpoint sets it yet.

Known limitation, not a bug:
- Claiming the same draft twice returns 404 rather than the memoir it already
  created. `memoir` has no `from_draft_id` column, so the second request has no
  way to find the first one's result. Making it idempotent would need a schema
  change; the frontend should call `GET /me` instead.

Resolved:
- `subject_is_living` is now in `DraftUpdate`, and migration
  `0002_living_nullable.sql` is applied. The years wheel's "Present" option
  persists as `subject_is_living = true` with a null `through_year`; picking a
  year gives `false` plus that year; never touching the wheel leaves both null.
  Nullable because "we didn't ask" and "no, they have died" are different
  answers. Postgres enforces the pairing via `draft_living_has_no_end_year`.

Frontend wiring (`memoirproject-frontend`):
- The onboarding flow is connected end to end, pledge screen through to the
  invite link on the dashboard. The browser talks to Supabase Auth directly and
  sends the resulting token here; this API only verifies.
- The frontend feature folders mirror this repo's layers:
  `features/onboarding/` (owner, client data path — its only pre-signup
  credential lives in localStorage) and `features/invitation/` (contributor,
  server data path, because `GET /j/{token}` needs no auth).
- Per-step draft saves are best-effort and not awaited; the complete answer set
  is re-sent immediately before `claim`, so a dropped save cannot corrupt the
  memoir.

## Database conventions (already established — follow them, don't relitigate)

- Migrations are numbered SQL files in `migrations/`, wrapped in
  `BEGIN;`/`COMMIT;`, and **immutable once applied**. A schema change is
  always a new file, never an edit to an old one.
- Enums for closed, product-fixed vocabularies (roles, statuses). Adding a
  value is cheap; removing one is a rewrite, so start narrow.
- Foreign key `ON DELETE` behavior is chosen deliberately per relationship:
  `RESTRICT` when the child is more valuable than the parent (e.g. you cannot
  delete a `user_account` that owns a `memoir` — that would silently destroy
  a family's contributions), `CASCADE` when the child is meaningless without
  the parent, `SET NULL` when the child should outlive the parent minus the
  reference.
- `CHECK` constraints enforce product rules at the data layer, not just in
  application code, so an invalid state is impossible to store regardless of
  which code path writes it. Example: a living subject cannot have a death
  year (`draft_living_has_no_end_year`).
- Composite unique keys like `UNIQUE (memoir_id, id)` on `memoir_participant`
  exist so later tables can foreign-key on `(memoir_id, participant_id)` and
  make cross-memoir data leakage impossible at the database level, not just
  by convention.
- Partial unique indexes (`WHERE role = 'owner'`) enforce cross-row rules
  that a `CHECK` constraint cannot express, since `CHECK` only sees one row.

## API conventions

- Never build SQL by interpolating values into a string. Always use
  `%(name)s` parameter placeholders and pass values separately.
- No bare `except Exception: return 500`. Catch specific error types (see
  the `psycopg.Error` handler in `main.py`) and let anything unanticipated
  re-raise as a real 500 with a traceback — that's a bug you want to see, not
  a bug you want hidden.
- Use `exclude_unset=True` on Pydantic partial updates, or you'll overwrite
  every unmentioned field with null.
- Validate at two layers, not one: Pydantic/`Literal` types at the API
  boundary for good error messages, and a database constraint underneath for
  the actual guarantee, since the constraint holds even if some future code
  path forgets to validate.
- Prefer 400 for malformed input, 404 for "not found or not yours" (never
  leak which one — a wrong token should look identical to a missing
  resource), 409 for a conflict with existing state, 401 for missing/invalid
  auth.
- Anything that writes multiple related rows (see `claim`, once built) must
  be one database transaction. Partial writes on failure are not acceptable
  — a memoir with no owner row is an orphan nobody can reach or clean up.

## How to work with the maintainer

The person running this session is learning backend development as they
build. This changes your job:

- **Explain before or alongside writing, not after.** State what you're
  about to do and why in plain language before producing code. Assume no
  prior backend experience — define terms like "transaction," "migration,"
  "dependency injection" the first time each comes up in a session.
- **Prefer small, reviewable changes over large rewrites.** One endpoint or
  one concept at a time. If a task naturally splits into "the boilerplate
  part" and "the part that's conceptually new," say so explicitly — the
  maintainer wants to write the new-concept part themselves in many cases,
  not have it handed over solved.
- **When something breaks, don't just fix it — explain what the error
  meant.** A traceback is a teaching opportunity; read it bottom-up and show
  which line is the real culprit versus framework noise.
- **Don't silently deviate from the conventions above.** If you think a
  convention here is wrong for a specific case, say why and ask, rather than
  quietly doing something different.
- **Do not add features, tables, or endpoints beyond what's asked.** This
  project deliberately builds in thin vertical slices — one working path end
  to end — rather than broad scaffolding. Resist the instinct to "complete"
  something further than requested.

_(empty — add your instructions above this line)_

---

## How this codebase works

Everything below is what I worked out from reading the repo. Correct anything that's wrong.

### The layering rule

Requests flow one direction: **`api/` → `domain/` → `integrations/`**

- **`src/api/`** — FastAPI routers. Translates HTTP and nothing else: read the request, call
  `domain/`, turn the result into a status code. Must not contain SQL.
- **`src/domain/<feature>/`** — the business logic and the SQL. Must not import `fastapi`; this
  layer does not know what a 404 is. It returns `None` or raises its own errors, and the route
  decides what that means over HTTP.
- **`src/integrations/`** — thin wrappers around external services (Postgres, Supabase, LLMs).
  No feature knowledge. If the word "memoir" appears here, it's in the wrong file.
- **`src/models/`** — Pydantic request/response models, one file per feature.
- **`src/core/`** — app-wide setup: config, logging, lifespan hooks.
- **`src/utils/`** — generic helpers not tied to a feature.

The PR review bot in `.github/workflows/opencode.yml` checks this, and so does `README.md`.

Quick self-check (ignore hits inside comments and docstrings — the files
explain these rules in prose, so a plain grep matches its own documentation):

```bash
grep -rniE "^[^#]*\b(select|insert|update|delete) " src/api/ --include=*.py
grep -rn "^\s*\(from\|import\) fastapi" src/domain/ --include=*.py
```

Both should come back empty.

### Adding a feature

Three files, same name in each place:

```
src/models/<feature>_models.py       the request/response shapes
src/domain/<feature>/<feature>_service.py    the logic and the SQL
src/api/<feature>.py                 the routes
```

Then register the router in `src/main.py`. `src/main.py` is wiring only — if you're writing
`@app.get` in it, that route belongs in `src/api/` instead.

### Code style

The `example_*` files under `src/` are leftover template scaffolding kept as a reference. Where
they disagree with the real Memoir code, **the Memoir code wins**:

- `str | None`, not `Optional[str]`.
- `def` route handlers, not `async def` — psycopg is synchronous here, and `def` lets FastAPI run
  the handler in a threadpool instead of blocking the event loop.
- No `__init__.py` files. `src/` uses implicit namespace packages.

### Config

All environment variables are read **once**, in `src/core/config.py`, into a `settings` object.
Nothing else calls `os.environ` or `load_dotenv()`. A missing variable crashes the app at startup
on purpose — better than failing on the first request.

### Database

- Connect via `db()` from `src/integrations/db.py`. It returns `dict_row` rows, which is why
  handlers can return a row straight to FastAPI as JSON.
- **Every query must be filtered by its owning id** — `memoir_id`, or a draft's `id` + `token`.
  Row Level Security is enabled on all five tables with **zero policies**, and the API connects
  with a role that bypasses RLS. That means the route handlers are the *only* thing protecting
  this data. A query missing its ownership filter is a data leak, not a bug.
- Anonymous drafts are authenticated by a secret token sent in the `X-Draft-Token` header, not a
  cookie — the frontend and API are on different origins.

### Migrations

Hand-run, numbered, forward-only. There is no history table and no down-migrations.

```bash
python migrate.py migrations/0001_slice1.sql
```

Write a new numbered `.sql` file in `migrations/` and run it. `migrations/0001_slice1.sql` has
extensive comments explaining why the schema is shaped the way it is — read it before changing
anything about the tables.

### Running it

```bash
uvicorn src.main:app --reload
```

From the `memoirproject-ai-backend/` folder, with `.venv` active. This is what the `Dockerfile`
and `README.md` use.

### Gotchas

- `requirements.txt` used to be UTF-16LE (from a PowerShell `pip freeze >`) and read as binary
  garbage. It has been rewritten as UTF-8. If you regenerate it from PowerShell, use
  `pip freeze | Out-File -Encoding utf8 requirements.txt` or the redirect will undo that.
- `.python-version` says 3.11 and the Dockerfile uses 3.11, but the local `.venv` is 3.13.
- There is currently **no test suite**, and CI (`.github/workflows/ci.yaml`) is a placeholder that
  runs `echo "Add test/lint commands here"`.
- Auth needs `PyJWT[crypto]` — plain `pip install PyJWT` omits `cryptography` and ES256
  verification fails at runtime, not at import.

### Known gaps

- `subject_is_living` is missing from `DraftUpdate` in `src/models/draft_models.py`. The
  onboarding's years wheel has a "Present" option that means exactly this, so that answer
  currently cannot be saved. Migration `0002_living_nullable.sql` is already applied, so adding
  the field is a one-line change. Deferred deliberately.
- There is still **no test suite**. `.github/workflows/ci.yaml` runs
  `echo "Add test/lint commands here"`. Slice 1 was verified by an end-to-end script driving the
  real API against the real database, not by anything that runs in CI.
- CORS is still `allow_origins=["*"]`. Tolerable while auth is a header token and not a cookie;
  must become the real frontend origin before deploying.
- `db()` opens a fresh connection per call with no pooling. Fine at current traffic; when it
  stops being fine, a pool goes in `src/integrations/db.py` and every caller keeps working.
