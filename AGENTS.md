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

Slices 1 and 2 are complete end to end. Slice 2 is what makes the invite link
do something: memories, media, contributors and plans.

**Onboarding and identity**
- `GET /health` — confirms the API can reach Postgres
- `POST /drafts`, `PATCH /drafts/{draft_id}` — pre-signup answers, authorized
  by `X-Draft-Token`, not a login
- **Supabase JWT verification.** The project signs with **ES256** and publishes
  a JWKS, so the API verifies against Supabase's *public* key and holds no
  secret capable of forging a token. Keys cached in-process for 5 minutes.
  `leeway=30` on the decode, because Supabase's clock runs slightly ahead of
  ours and without it the *first* request made with a freshly minted token 401s
  — which lands squarely on `POST /memoirs/claim`.
- `POST /memoirs/claim` — the atomic transaction turning a draft plus an
  authenticated user into `user_account` + `memoir` + owner participant + link
- `GET /me` — identity plus owned memoirs. 200 with an empty list for someone
  who signed up and claimed nothing; a normal state, not a 404.
- `POST /dev/signin` — dev-only token minting, registered only when
  `ENABLE_DEV_ROUTES=true`. The import sits inside the `if`.

**Memories and media** (migration `0003`)
- `GET`/`POST /memoirs/{id}/memories`, `GET`/`PATCH`/`DELETE /memories/{id}` —
  the owner's side. The single `GET` exists for the drill-down page: the list
  already carries everything, so it is only reached by a link opened directly
  or a refresh.
- **One memory holds writing, photographs and recordings together**, in any
  combination. `media_asset.memory_id` was always one-to-many; what changed is
  that `memory.kind` stopped being a field the client sends.
- **`kind` is derived, never chosen** — `_derive_kind()` reads what the memory
  actually holds: any audio → `voice`, else any image → `photo`, else `text`.
  It is the *primary medium*, and its only job is the eyebrow on an archive
  card. No `mixed` value: that would need a migration for a word nobody should
  read above their grandmother's memory.
  The asset query is filtered on `memoir_id`, so an id from another memoir
  matches nothing and cannot influence the answer.
- **A memory must hold something.** No text and no assets raises `EmptyMemory`
  → **400**. Note this and the database's `memory_text_has_body` agree by
  construction: `text` is derived only when no asset survived the filter, so a
  `text` row must carry words or it was rejected a line earlier.
- `POST /media/uploads` + `/complete` — signed direct-to-storage uploads.
  Accepts **either** an owner's bearer token or a contributor's `X-Link-Token`.
- `POST /j/{token}/memories` — a contributor with no account leaves a memory
  and receives a `participant_token`, so a return visit is the same person.
- `GET /j/{token}/memories` — scoped to that one participant. A contributor can
  see what they added and nothing else.

**Contributors and plans** (migrations `0004`, `0005`)
- `GET /memoirs/{id}/contributors` — participants with memory counts, plus the
  live link. Never exposes `contributor_token`.
- `POST /memoirs/{id}/link/reissue` — revoke and replace, in one transaction.
- `GET /plans` — **public.** The price list. Public because onboarding's pricing
  screen reads it, and that screen should not depend on where it happens to sit
  relative to signup.
- `GET /billing` — the plan and a **real** storage meter, summed from confirmed
  uploads.
- `PATCH /billing/plan` — moves the account onto a term. An *entitlement*
  change, not a charge: the response still reports `payments_enabled: false`
  with no renewal date. It exists so the billing screen quotes the term chosen
  on the pricing screen. 404 covers both "no account yet" and "no such plan",
  deliberately undistinguished.

Keepsake bills monthly ($3) or yearly ($30) — two `plan` rows sharing a name, a
tagline and a 10 GiB entitlement, differing only in `billing_interval`. Two rows
rather than a second price column because `user_account.plan_code` has to point
at exactly one of them, and a column cannot be pointed at.

**Transcription** (migration `0006`)
- Every confirmed **audio** upload is submitted to AssemblyAI automatically,
  from a `BackgroundTask` at the end of `POST /media/uploads/{id}/complete`, so
  confirming an upload stays as fast as it was.
- **We send a signed URL, not bytes.** AssemblyAI fetches the object from
  storage itself, so the audio never passes through this API a second time.
- `POST /webhooks/assemblyai` — public, authenticated by a secret *we* invent
  and submit with each job, compared back with `hmac.compare_digest`. Returns
  200 for everything except a bad secret: a webhook sender reads a non-2xx as
  "retry", so an endpoint that 500s on a payload it dislikes arranges to be
  sent that payload for hours.
- **The webhook has a twin.** `refresh_pending()` runs on both memory-list
  routes and chases anything still `processing` after ~8s, bounded to 3 jobs
  per request. It costs nothing when nothing is pending, it is how a laptop
  with no public URL gets transcripts at all, and it is the safety net for a
  webhook that got lost. Both paths write through the **same** `apply_result()`
  — one function, two callers, so they cannot drift.
- The transcript reaches the frontend on `MediaAsset.transcript`. `provider_id`
  and `error` are filtered out by `response_model`, like `storage_path`.

Stored: text plus **paragraph** segments. Not word-level timings — that array is
~750 KB per audio hour, fifteen times the transcript itself. See the header of
`src/integrations/assemblyai.py`.

`language_detection` is on rather than a hardcoded `en`. These subjects do not
all speak English, and asking for English transcription of Urdu does not fail —
it returns fluent, confident nonsense, which is worse.

Throughout: a central `psycopg.Error` handler mapping SQLSTATE codes to clean
4xx JSON responses instead of raw 500s, in `src/core/error_handlers.py`.

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
    supabase_storage.py              signed upload/download URLs, object_size()
    assemblyai.py                    submit(), fetch(), paragraphs()
  models/
    draft_models.py                  DraftUpdate
    memoir_models.py                 ClaimRequest, MemoirSummary, AccountOverview,
                                     LinkInvitation
    memory_models.py                 Memory, MemoryCreate, ContributedMemory,
                                     MediaAsset, Transcript, UploadRequest/Ticket,
                                     StorageUsage
    account_models.py                Contributor, ShareLink, Plan, BillingOverview
  domain/
    drafts/draft_service.py          create_draft(), update_draft()
    memoirs/memoir_service.py        claim_draft(), list_memoirs_for_owner();
                                     raises AlreadyHasMemoir (one per account)
    memoirs/access.py                owned_memoir(), contributable_memoir() —
                                     the two ways to prove you may write
    links/link_service.py            resolve_link()
    memories/memory_service.py       memories, owner-side and contributor-side;
                                     _derive_kind() decides what a memory *is*
    media/media_service.py           begin_upload(), complete_upload()
    contributors/contributor_service.py  list_contributors(), reissue_link()
    billing/billing_service.py       get_billing_overview(), list_plans(), set_plan()
    transcripts/transcript_service.py  request_transcription(), apply_result(),
                                     reconcile(), refresh_pending()
  api/
    dependencies.py                  current_user -> CurrentUser (401s live here)
    health.py                        GET  /health
    drafts.py                        POST /drafts, PATCH /drafts/{draft_id}
    memoirs.py                       POST /memoirs/claim          (auth)
    accounts.py                      GET  /me                     (auth)
    links.py                         GET  /j/{token}              (public)
    memories.py                      memories, both audiences
    media.py                         uploads, either credential
    contributors.py                  contributors list, link reissue (auth)
    billing.py                       GET  /plans                  (public)
                                     GET  /billing, PATCH /billing/plan (auth)
    webhooks.py                      POST /webhooks/assemblyai    (secret header)
    dev.py                           POST /dev/signin             (gated)
```

The `example_*` files alongside these are leftover template scaffolding, kept
as a reference. They are not wired into the app.

Environment variables (`.env`): `DATABASE_URL`, `SUPABASE_URL`,
`SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `ENABLE_DEV_ROUTES`,
`ASSEMBLYAI_API_KEY`, `ASSEMBLYAI_WEBHOOK_SECRET`, `PUBLIC_BASE_URL`,
`TRANSCRIPTION_ENABLED`.

`PUBLIC_BASE_URL` is deliberately **empty in local development** — localhost is
not reachable from the internet, so no webhook is requested and the poll path
does the work. No tunnel is needed to develop or test this.

The first three are **not** secrets — the URL and anon key ship in every
frontend bundle, and token verification uses the public JWKS. The service role
key **is** a secret, is the only one this app holds, and exists for exactly one
reason: signing upload URLs for contributors, who have no token of their own.
It is confined to `src/integrations/supabase_storage.py`. Never log it, never
return it, never send it to the browser.

Storage: a private Supabase bucket named by `SUPABASE_STORAGE_BUCKET`
(default `memoir-media`). The database holds paths and metadata; the bytes
never pass through this API in either direction.

Not built yet:
- **Stripe.** `GET /billing` reports `payments_enabled: false` and a null
  renewal date, and `PATCH /billing/plan` takes no money — it records which term
  was chosen so the billing screen agrees with the pricing screen. Migrations
  `0004`/`0005` deliberately have no `subscription` table — the entitlement
  (what you get) is separate from the subscription (what you pay), so adding
  payments adds a table beside `plan` and changes nothing here.
- Chapters, comments, publishing, and PDF export.
- **Chapter assembly.** Transcription is done; turning many contributions into
  chapters is not. That step reads the whole archive — typed memories and photo
  captions included, which AssemblyAI has never seen — so it is a direct Claude
  call, not AssemblyAI's LeMUR. Deliberately no `entity_detection`,
  `summarization` or `auto_chapters` on the transcript job: each is billed per
  audio hour on top, and the assembly step extracts the same facts across more
  material.
- **Transcript editing.** Transcripts are machine input, read-only. Correction
  happens once, at the assembly step, rather than by asking a grieving family to
  proofread every recording.
- Google OAuth. Only the email provider is enabled on the Supabase project. A
  Google-issued JWT verifies identically, so nothing here changes.
- **Cleanup of abandoned uploads.** An upload reserved and never confirmed
  leaves a `media_asset` row with a null `uploaded_at`. It counts towards
  nobody's storage, but it accumulates. Needs somewhere to run scheduled work.
  See the note at the end of `migrations/0003_memories.sql`.

Frontend wiring (`memoirproject-frontend`):
- Connected end to end: onboarding → signup → archive → invite link →
  contributor adds a voice note or a photograph → the owner sees it.
- Its feature folders mirror this repo's domain folders — `account/`,
  `archive/`, `contributors/`, `billing/`, `media/`, `invitation/`,
  `onboarding/`. `invitation/` is the only one with a server data path, because
  `GET /j/{token}` is the only endpoint needing no credential.
- The browser talks to Supabase Auth directly and sends the resulting token
  here; this API only verifies. Uploads go **straight from the browser to
  storage** using a URL this API signs — the bytes never pass through here.
- Per-step draft saves are best-effort and not awaited; the complete answer set
  is re-sent immediately before `claim`, so a dropped save cannot corrupt the
  memoir.

**One memoir per account** (migration `0007`)
- `memoir_one_per_account`, a UNIQUE index on `memoir(created_by_user_id)`.
- `claim_draft` checks it first and raises `AlreadyHasMemoir` → **409**, so the
  frontend can say "you already have a memoir" and link to the archive rather
  than showing the generic `already_exists` the 23505 handler would produce.
- **Why it exists:** `useActiveMemoir` renders `memoirs[0]`. A second memoir
  silently became the visible one and every memory in the first stopped being
  rendered — not deleted, just unreachable. One test account had five memoirs
  and could see one. That is data loss wearing the costume of a display bug.
- Precedence: the guard runs before the draft is read, so an account that
  already has a memoir gets 409 whatever is wrong with the draft. A **fresh**
  account still gets 404 for a bad token and 400 for a nameless draft — both
  covered by the slice-1 script.
- Claiming the same draft twice is now 409 (was 404). The more useful of the
  two: they already have what they were making.

**Transcription budget** (migrations `0008`, `0009`)
- `plan.transcription_minutes`, 600 for both Keepsake terms. Past it,
  `request_transcription` records `skipped` — not `failed`, so no retry pass
  ever spends it. The frontend says so plainly; a family that hits a ceiling
  should know why one recording has words and another does not.
- Consumption is **summed, never counted**: a counter has to survive a failed
  job, a deleted memory and a duplicate webhook, and eventually it does not.
- It prefers `transcript.audio_seconds` — which AssemblyAI reports — over
  `media_asset.duration_ms`, which the **client** sends. A budget summed from a
  number the limited party chooses is not a budget. The client's estimate still
  gates admission, because the true length is unknown until a job finishes, so
  understating a duration buys exactly one recording before the ledger corrects
  itself. Same reasoning as `byte_size` asking storage instead of the uploader.

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
- **Clock skew is not hypothetical.** Supabase issues tokens with an `iat` about a second ahead
  of this machine, and PyJWT refuses a token whose `iat` is in the future. The symptom is bizarre:
  the *first* request made with a freshly minted token 401s and every one after it succeeds.
  `verify_access_token` passes `leeway=30` for this. Do not remove it.
- Signed storage URLs are minted **outside** the database transaction that creates the asset row.
  Holding a Postgres connection open across an HTTP call to another service is how a connection
  pool dies. The cost is an orphaned reservation row if signing fails, which is harmless.

### Known gaps

- There is still **no test suite**. `.github/workflows/ci.yaml` runs
  `echo "Add test/lint commands here"`. Both slices were verified by end-to-end scripts driving
  the real API against the real database and real storage — 70 checks covering the happy paths,
  the ownership boundaries (a stranger gets 404, never 403), the immutability rule (writing to a
  published memoir is 409), revoked links, and the upload allow-list. None of it runs in CI, and
  none of it lives in the repo.
- **Storage cost is unbounded per account.** `plan.storage_limit_bytes` is stored and displayed,
  and `GET /billing` reports real usage against it, but nothing refuses an upload that would
  exceed it. The check belongs in `begin_upload()`.
- Transcription spend is now bounded by `plan.transcription_minutes` (600).
  `TRANSCRIPTION_ENABLED=false` remains the blunt kill switch.
- **An abandoned upload is still transcribed.** Transcription fires when the
  object is confirmed in storage, which is before a memory adopts it, so a
  reservation nobody attaches to anything is paid for once. Same orphan class as
  above, now with a cost attached.
- **A failed transcript is never retried.** Nothing re-submits it.
- CORS is still `allow_origins=["*"]`. Tolerable while auth is a header token and not a cookie;
  must become the real frontend origin before deploying.
- `db()` opens a fresh connection per call with no pooling. Fine at current traffic; when it
  stops being fine, a pool goes in `src/integrations/db.py` and every caller keeps working.
- A contributor's `participant_token` never expires and cannot be revoked individually. Revoking
  the share link stops new contributions from everyone at once, which is the only lever there is.
