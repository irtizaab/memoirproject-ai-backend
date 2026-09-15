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
  material after publication means a new memoir, not an edit. Before sealing
  the opposite holds: the owner may change anything by hand, and
  `PATCH /chapters/{id}` is how. Immutability is a property of **sealing**, not
  of assembly — conflating the two left owners unable to fix one sentence in
  their own draft.
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

**The finished memoir** (migrations `0011`, `0012`)
- `POST /r/{token}/open` — **the door**. Takes the passphrase and a name,
  returns a reader session. Everything that fails answers 404: unknown token,
  revoked, wrong scope, no passphrase set, wrong passphrase. Telling a real
  link with a bad passphrase apart from a link that was never real is what
  turns a leaked link into a target. The owner is recognised by their bearer
  token and let through without either — they set the passphrase and their
  account carries their name.
- `GET /r/{token}` — resolves a **view**-scoped `memoir_link` into the book's
  covers: subject, contents, the index of people, the colophon's four numbers.
  Needs `X-Reader-Token` as well as the link; the link says which memoir, the
  session says who is holding it. `response_model` keeps `never_forget` out of
  it, for the same reason `LinkInvitation` does.
- `GET /memoirs/{id}/chapters` — the same covers for the owner (auth), so they
  can read before publishing and without a view link existing.
- `GET /chapters/{id}`, `GET`/`POST /chapters/{id}/comments` — **either**
  credential, the posture `media.py` already takes: an owner's bearer token, or
  `X-Link-Token` **and** `X-Reader-Token`. A second parallel public router was
  the alternative, and that is how two paths meant to return the same thing
  drift.
- **Nobody reads anonymously, and nobody signs a comment twice.** Identity is
  taken once, at the door, and `CommentCreate` carries no name at all. Before
  that, a person could read the whole book as nobody and be asked who they were
  only if they had something to say.
- **The session is a signature, not a row** (`domain/chapters/reader_gate.py`):
  `{participant_id}.{hmac}`, keyed on the live link token and the passphrase
  hash. So replacing the passphrase or revoking the link closes every session
  that was ever issued, with nothing to sweep. It also sidesteps the CHECK in
  `0003` that forbids an owner from holding a `contributor_token` — the owner
  has to be able to read their own memoir.
- **Scope is load-bearing.** `readable_memoir()` requires `scope = 'view'`, next
  to `contributable_memoir()`'s `scope = 'contribute'`. A link posted in a
  family group chat so people can send memories must not also hand out the
  finished book, and vice versa.
- **A chapter is blocks, not a string.** `chapter_block` holds paragraphs,
  pulled lines and figures in reading order; `block_source` says which *memory*
  and which *person* each passage came from, down to a range of characters.
  That is what makes "never fabricate" checkable by a reader rather than merely
  asserted, and `diverges` is how the divergent-accounts rule is stored rather
  than argued about.
- **Character offsets are safe here because publication is immutable.** The text
  can never move out from under an anchor, so there is no rebasing and no
  orphaned comment. If a published chapter ever becomes editable, `block_source`
  and `comment_thread` both need rewriting.
- **A figure's caption is the contributor's own words**, read from the memory
  the photograph came from. Nothing generates one — inventing a description of
  somebody's photograph is exactly what the fabrication rule forbids.
- **`POST /chapters/{id}/comments` is the only write a published memoir
  accepts.** Everything else answers 409 once `status` flips. This is the layer
  the confirm screen makes people tick a box about, and refusing it here would
  break that sentence.
- Comments are attributed to a `memoir_participant`, never a `user_account`,
  and a returning reader is recognised by the same `contributor_token` they
  already hold — `resolve_participant()`, shared with the memories path rather
  than copied.
- Nothing here **writes** a chapter: `assembly_service` builds them from the
  archive and `page_service` applies the owner's hand corrections. What this
  slice settled is the shape both of them have to emit, which is the part that
  gets expensive to change once a prompt is written against it.

**AI: the question library and assembly** (migration `0014`)

One integration, `src/integrations/gemini.py`, spent on two things — and
since the guide, two providers: a model named `groq:<model>` in either
`GEMINI_MODEL` or `GEMINI_ASSEMBLY_MODEL` goes to Groq's OpenAI-shaped endpoint
with `GROQ_API_KEY`, through the same `generate()`, the same retry and the same
errors; nothing in `domain/` can tell. Groq takes JSON Schema as Pydantic
writes it (no OpenAPI rewriting) and five images per request, so an archive
with more photographs has the rest placed by the positional rule. It is a
direct REST call through the shared `httpx.AsyncClient`, **not** the
`google-genai` SDK — that client is synchronous by default and would block the
event loop, which is what `test_no_blocking_io_in_src` exists to catch. It takes
a Pydantic class and returns an instance of it; there is no path that returns
free text. `AI_ENABLED` is the kill switch, the same shape as
`TRANSCRIPTION_ENABLED`, and `GEMINI_API_KEY` is a secret confined to that one
module.

Gemini rather than Claude. The line this file used to carry — "a direct Claude
call, not AssemblyAI's LeMUR" — was about making a *direct model call* over
material AssemblyAI never sees (typed memories, photo captions), and that
reasoning is unchanged. The vendor is not. Still deliberately no
`entity_detection`, `summarization` or `auto_chapters` on the transcript job:
each is billed per audio hour on top, and assembly extracts the same facts
across more material.

- **The question library.** The owner writes a line about the subject on
  `/questions`, a model drafts four or five plain questions for each
  `relationship_group`, and they edit any of them by hand or ask for a fresh
  set. `GET`/`POST /memoirs/{id}/questions{,/generate}`, `PATCH
  /memoirs/{id}/questions/mode`, `PATCH`/`DELETE /questions/{id}`, and
  `GET /j/{token}/questions` for the contributor.

  **The owner is in the middle of it, and that is the design.** An earlier
  version wrote a question per contributor out of the memory they had just
  left, and the owner never saw any of it — a model deciding, unsupervised and
  one person at a time, what a grieving family would be asked about somebody
  they had lost. The model drafts, the owner reads and edits, and only then is
  anybody asked anything.

  **Two sets, and neither is "off".** `questions_mode` is `standard` or
  `custom`. Standard is the constant in `domain/prompts/standard_questions.py`
  — written, not generated, and covering every group — and it is the default,
  so a memoir whose owner never opens the screen still shows contributors
  something. It is also the fallback for every failure and for a group the
  owner emptied, because the blank page is the problem this feature exists to
  solve. Switching modes destroys neither set.

  **`source` is what makes editing safe.** A question becomes `'owner'` the
  moment it is edited by hand, and a rewrite replaces the `'ai'` rows and
  leaves those alone. Losing a generated question costs a model call; losing a
  hand-written one costs the sentence a person chose. `replace_edited` is the
  owner saying "start again" and is never the default.

  **`never_forget` is finally consumed.** Onboarding's last screen has always
  promised "it becomes the first question your family is asked", and nothing
  read it. `_describe()` sends it as the seed for the first question of every
  group. It reaches the model and no contributor — it stays excluded from every
  response model a link can reach, exactly as `LinkInvitation` and
  `MemoirReading` exclude it.

  **A contributor sees text and nothing else.** `ContributorQuestions` declares
  the questions and the group name; no ids for rows they cannot edit, no
  `source` saying which a model wrote, no mode, and above all not
  `subject_notes`. Generation fails as **503**, not 500: nothing is broken, an
  upstream service was away, and the notes are saved before the call so a
  failure never costs the owner what they typed.

  **This needed the contribute form to start asking who somebody is.**
  `resolve_participant()` wrote `'other'` for every contributor, hardcoded, so
  a per-group library would have collapsed to one set. `ContributedMemory` now
  carries an optional `relationship`, written through on create and on return
  visits the same way `display_name` is. `None` means "leave it alone", not
  "reset to other". It also fixes the reader's credit lines, which read "other"
  under everybody's name.

- **Chapter assembly, in three steps** (migrations `0015`, `0016`).
  `POST /memoirs/{id}/plan` reads the archive and decides what the book is;
  `GET`/`PATCH` on the same path read and correct that decision;
  `POST /memoirs/{id}/assemble` writes it into chapters. `planner.py` holds the
  prompt, the schema and the verification, `plan_service.py` owns the
  `memoir_plan` row, and `assembly_service.py` still owns every write.

  **Why the plan is a row now.** It used to be a local variable: the model
  decided how a family's memoir divided into chapters and what each was called,
  that went straight into the book, and the owner saw four counts. They could
  not read the outline, rename a chapter, or tell whether the model had run.
  Same argument migration `0014` makes about the question library, one step
  later — written once, read before it is used, changed by hand when wrong.

  It also moved the five-minute model call out of the assemble request.
  `assemble()` is now Postgres end to end, and back to a single transaction.

  **`organised_by` is on every response.** `planner` or `by_date`. Without it a
  decade-banded book and a planned one are indistinguishable once written,
  which is exactly how a deployment with no `GEMINI_API_KEY` produced the
  fallback for every memoir and said nothing about it.

  **The plan stays a draft until the memoir is sealed** — not until it is
  assembled, which is what this used to say. Assembly is not what makes a
  character offset permanent; publication is, and an unsealed book is rewritten
  wholesale by the next `assemble()`. So the owner can keep correcting the
  outline and press assemble again to apply it. `PATCH` may rename a chapter,
  reorder or drop one, reorder, reword or drop a passage, and move or drop a
  photograph. It may **not** reassign attribution: `block_source`'s memories are
  read from the stored plan and never from the request. Chapter order is array
  position, renumbered server-side; a chapter id the plan does not hold is a
  400.

- **Planning is three agents, not one call** (migration `0017`). All in
  `planner.py`, all through the same `gemini.generate`, none trusted.

    the builder    drafts the plan on `gemini_assembly_model`, as before.
    the reviewer   reads the draft against the archive on `gemini_model` and
                   returns `findings` (`attribution`, `structure`,
                   `contradiction`, `instruction`, `thin`) plus, if it would
                   change anything, a revised plan. The revision goes through
                   the same `_clean`/`verify` as the draft and is taken only if
                   `_accept_revision` says it attributes no worse — a model
                   asked to remove and re-word can still lose a quote. The
                   findings are stored on the plan and read out in the chat.
    the guide      talks to the owner and **never sees a memory's words** —
                   `_shape` hands it counts, dates and contributors; after
                   planning it also sees numbered chapter titles, the review
                   and the builder's `reason`. It runs once, after the
                   builder, to write the plain-language `guide` note; the
                   `instructions` the builder gets arrive already restated by
                   the chat turn that asked for a replan (`POST /plan` takes
                   no body), so nothing runs before the builder.
                   `tests/unit/test_planner.py` pins the surface.

  `planner.plan()` returns an `Outcome` — `chapters`, `reason`, `review`,
  `guide` — and `plan_service.store` keeps the last three inside the plan's
  jsonb `body`, next to the chapters they describe, because they are replaced
  with them. Each agent that fails costs only what it adds: no reviewer means
  the draft stands, no guide means the owner reads `reason`. There is no
  outline panel: `chat_service.describe()` writes the plan out as numbered
  chapters plus the note and the findings, and `POST /plan` stores that as a
  guide message (`announce()`), so a book built from the button and one built
  in conversation read the same way in the transcript.

  **And the guide takes questions.** `GET`/`POST /memoirs/{id}/chat`,
  owner-bearer only, 409 once sealed, in `chat_service.py` with
  `memoir_chat_message` behind it. The guide answers in plain words, and when
  the owner asks for a different *division* of the memories — what goes
  together, what is separate — it sets `replan` and `send()` runs
  `generate_plan` with the request restated, inside the same request (so the
  frontend allows the plan's timeout) and returns the new plan with the
  reply. It also renames, reorders and drops chapters itself: it is shown
  the outline as numbered titles and answers with an `outline` — the
  chapters to keep, in order, by number — which `chat_service._apply_outline`
  maps back to ids and puts through `plan_service.edit`, the same path
  `PATCH /plan` uses. A model is never handed a uuid to copy. After either
  kind of change `send()` runs `assemble()`, so the book the owner opens is
  the one they just talked about; the frontend's one "Build" button is
  `POST /plan` then `POST /assemble` for the same reason, and the outline
  editor it used to show is gone. The guide's reply is written *before* the
  builder runs, so it can
  only promise; `send()` appends the new plan's `guide` note — or its
  `reason`, when the builder fell back — so a replan that produced the
  decade outline says why in the chat and not only in the server log. When a
  request is ambiguous, or the archive cannot honour it as described, the
  guide asks one question first and replans on the answering turn. Renaming,
  reordering and dropping chapters it hands back to the outline. A guide that
  cannot answer is a stored message saying so and a 200, never a 500: the
  owner's question is kept.

  One replan is three calls on `GEMINI_MODEL` (the chat turn, the reviewer,
  the guide's note) and one on `GEMINI_ASSEMBLY_MODEL`. A free-tier key allows
  five flash requests a minute, which two sends in a row exceed: the guide
  and reviewer then log a 429 and the draft stands with no note. That is a
  billing ceiling, not a code path.

- **The page itself is editable too** (`chapters/page_service.py`).
  `PATCH /chapters/{id}` — owner bearer only, no link path, 409 once published.
  The owner reads the assembled memoir at the frontend's `/preview/{memoir_id}`
  and corrects what they find: the chapter title, the words of a passage, the
  order of the page, which paragraph a photograph sits beside and how it is
  shown, or whether any of it is there at all.

  **Two surfaces, on purpose.** The outline is where the *shape* is decided —
  which chapter a memory belongs in is a plan decision, and applying an outline
  means reassembling. The page is where the *words* are fixed, without spending
  a model call to change one of them. Reassembling replaces a hand-corrected
  page, and the frontend says so before the button is pressed; nothing in the
  API forbids it.

  **What it refuses is what protects the product's first rule.** No passage can
  be added — prose with no `block_source` behind it is a fabricated paragraph,
  and this path has no archive to attribute one to — and no passage moves to
  another chapter. A blank passage is a 400, not a silent delete; an emptied
  chapter is a 409, because a chapter with no prose cannot hold its photographs
  either.

  **Every edited passage is re-surveyed**, by the same `plan_service.resurvey`
  the plan uses — written once and imported, so attribution cannot come to mean
  two different things depending on which screen the owner used.
  `comment_thread` carries the same offsets and gets the same treatment:
  normally there are none yet, but an owner can comment on their own draft, and
  a comment pointing at moved words is the exact failure the offsets were
  checked to avoid.

  Ordinals are renumbered in two passes, parked high and then landed, because
  `UNIQUE (chapter_id, ordinal)` fires halfway through a reorder otherwise.

  **`plan_service.resurvey` is what makes editing safe.** A source's span is
  character offsets into passage text. Edit the passage and every offset past
  the edit is wrong — pointing at a clause somebody else said, permanently,
  because publication is immutable. So each span is re-*found*: take the exact
  substring the offsets covered, look for it in the new text, keep the span only
  if it is there exactly once, and otherwise fall back to whole-block
  attribution with NULL offsets. Same rule `verify` applies to an ambiguous
  quote, for the same reason.

  **A schema with an optional field used to fail silently, and did.**
  Gemini's `responseSchema` is an OpenAPI **3.0** subset: nullability is
  `nullable: true`, and `{"type": "null"}` — which is how Pydantic spells
  `str | None` — makes it answer **400** to the whole request. `planner.plan()`
  turns any failure into `None` and falls back, so `Plan`'s three optional
  fields meant the planner had never once run in production while `Library`,
  which has none, worked fine. `gemini._schema_for` now collapses the optional
  shape; a genuine two-type union is deliberately left to fail loudly rather
  than silently become one arbitrary half. `tests/unit/test_gemini.py` guards
  it.

  **The model never returns a character offset.** It returns `quote`, the exact
  substring of its own paragraph that came from one memory, and
  `planner.verify` computes the offsets here with `str.find`. An integer a model
  invents cannot be checked; a substring can. Found once → real offsets. Absent
  or appearing twice → whole-block attribution with NULL offsets, logged: the
  credit is still right and only the precision is lost, and a wrong offset is
  permanent because publication is immutable. An unknown `memory_id` is dropped.
  A block left with no valid source is dropped entirely — an unattributed
  paragraph in this product is a fabricated one. `participant_id` is always read
  from the archive row, never from the model.

  **The fallback is not a stub.** `_plan_by_date()` is the old decade-banding
  assembler, and it runs whenever the planner cannot: switched off, no key,
  unreachable, refused, or a plan that survived verification with nothing left.
  A memoir that cannot be assembled at all is unreachable — no reader, no
  export, no search — which is worse than a book divided by decade. It also
  cannot fabricate: it never rephrases, so every word is a word somebody typed
  or spoke.

  **Planning sits between two transactions, not inside one.** It can take
  minutes, and a Postgres connection held open across an HTTP call is how a
  bounded pool dies. The second transaction re-checks ownership and `status`
  before writing, so nothing reaches a memoir published in between. All the
  writes remain one transaction.

  **The model sees the photographs.** Every image in the archive is fetched
  from the private bucket, re-encoded to 768px JPEG, and sent as an inline part
  alongside the prompt, labelled with its `asset_id` so it can be named. It
  returns, per photograph, the memory whose paragraph it belongs beside and a
  placement: `margin`, `inset`, or `carousel` (migration `0016`) for several
  pictures of one moment shown in turn. `verify_figures` checks every asset id
  against what was actually read and every anchor against what the chapter
  quotes; `_write_chapter` resolves the anchor to a block it wrote itself, never
  to anything the model said about position. The old positional rule — first
  inset, rest margin — remains as the fallback for a `by_date` plan.

  **Three things had to change before it worked once, live.** All three failed
  silently into the decade fallback, which is why `organised_by` exists.

    `maxOutputTokens`   Unset means the model's default ceiling, and a plan is
                        longer than the archive it came from — every composed
                        paragraph plus a verbatim quote per source, with the
                        3.x thinking tokens drawn from the same allowance. Past
                        it the answer is not an error: it is JSON that stops
                        mid-string. `planner.MAX_PLAN_TOKENS` asks for the
                        tier's full 65,536, and `generate` now names a
                        `MAX_TOKENS` finish reason instead of reporting a
                        truncated document as a malformed one.
    the retry schedule  `_BACKOFF_SECONDS` is two steps and four seconds,
                        which is right for a contributor waiting on one
                        question and wrong for this: a transient "high demand"
                        503 gave up with five minutes of the call's own timeout
                        unspent. Assembly passes `gemini.PATIENT_BACKOFF`
                        instead — 150 seconds of waiting inside a 300-second
                        budget. Two 503s and a success is now an ordinary run.
    what the logs say   A `ValidationError` logged its own *count* ("1
                        problems"). A block dropped for having no text was
                        dropped in silence. Both now report shape and never
                        content: Pydantic's `loc` and `type`, and one line
                        counting chapters in/kept and blocks in/blank/
                        unattributed.

  Inline bytes are capped at 15 MB, roughly 150 photographs. Past it the rest
  are not looked at (logged, and still placed by the fallback rule); the upgrade
  path is Gemini's Files API, not a bigger number.

  A figure still anchors only to a `paragraph` — never to a `pull`, whose one
  lifted sentence would caption a photograph with a fragment. Captions are still
  the contributor's own words, read from the archive, and `_SYSTEM` forbids the
  model describing a photograph even though it can now see one.

Throughout: a central `psycopg.Error` handler mapping SQLSTATE codes to clean
4xx JSON responses instead of raw 500s, plus a `PoolTimeout` handler answering
503 when every connection is busy — both in `src/core/error_handlers.py`.

File layout:

```
src/
  main.py                            wiring only: app, CORS, error handlers, routers
  core/
    config.py                        settings.* — the only place env is read
    error_handlers.py                psycopg.Error -> 400/409, PoolTimeout -> 503
    app_lifespan.py                  startup/shutdown hook
    logging_config.py                setup_logging()
  integrations/
    db.py                            db() pooled connection, open_pool(), ping()
    http.py                          client() — the one shared httpx.AsyncClient
    supabase_auth.py                 verify_access_token(), password_signin()
    supabase_storage.py              signed upload/download URLs, object_size()
    assemblyai.py                    submit(), fetch(), paragraphs()
    gemini.py                        generate(prompt, schema, images=) ->
                                     schema. Typed JSON only; knows nothing
                                     about memoirs
  models/
    draft_models.py                  DraftUpdate
    memoir_models.py                 ClaimRequest, MemoirSummary, AccountOverview,
                                     LinkInvitation
    prompt_models.py                 QuestionLibrary, Question, StandardGroup,
                                     ContributorQuestions, Library
    memory_models.py                 Memory, MemoryCreate, ContributedMemory,
                                     MediaAsset, Transcript, UploadRequest/Ticket,
                                     StorageUsage
    account_models.py                Contributor, ShareLink, Plan, BillingOverview
    chapter_models.py                Chapter, Block, BlockSource, Figure,
                                     CommentThread, CommentCreate/Receipt,
                                     MemoirReading, MemoirPlan, PlanUpdate,
                                     ChapterEdit, BlockEdit, AssemblyResult
  domain/
    drafts/draft_service.py          create_draft(), update_draft()
    memoirs/memoir_service.py        claim_draft(), list_memoirs_for_owner();
                                     raises AlreadyHasMemoir (one per account)
    memoirs/access.py                owned_memoir(), contributable_memoir(),
                                     readable_memoir() — the three ways to
                                     prove you may reach a memoir
    links/link_service.py            resolve_link()
    memories/memory_service.py       memories, owner-side and contributor-side;
                                     _derive_kind() decides what a memory *is*
    prompts/prompt_service.py        get_library(), generate(), set_mode(),
                                     update_question(), for_contributor()
    prompts/standard_questions.py    the shipped set. Content, not code —
                                     and the fallback for every failure
    media/media_service.py           begin_upload(), complete_upload()
    contributors/contributor_service.py  list_contributors(), reissue_link()
    billing/billing_service.py       get_billing_overview(), list_plans(), set_plan()
    transcripts/transcript_service.py  request_transcription(), apply_result(),
                                     reconcile(), refresh_pending()
    chapters/chapter_service.py      reading_for_link()/for_owner(),
                                     get_chapter(), list_threads(), add_comment()
    chapters/assembly_service.py     generate_plan() and assemble(); _plan()
                                     dispatches, _plan_by_date() is the
                                     fallback, _photographs() reads the images
    chapters/page_service.py         edit_chapter(): the assembled page as the
                                     owner corrects it by hand, spans re-found
    chapters/chat_service.py         the owner's conversation with the guide;
                                     send() may run generate_plan()
    chapters/plan_service.py         the memoir_plan row: store/load/edit,
                                     rehydrate(), and resurvey() — the rule
                                     that keeps attribution true across an edit
    chapters/planner.py              the three agents — builder, reviewer,
                                     guide (and chat) — their prompts and
                                     schemas, and verify()/verify_figures():
                                     quotes and asset ids in, offsets and
                                     anchors out
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
    chapters.py                      GET  /memoirs/{id}/search     (auth)
                                     GET  /r/{token}/search  (link + session)
                                     GET  /memoirs/{id}/export.pdf (auth)
                                     POST /r/{token}/open         (the door)
                                     GET  /r/{token}    (link + reader session)
                                     GET  /memoirs/{id}/chapters  (auth)
                                     POST/GET/PATCH /memoirs/{id}/plan (auth)
                                     GET/POST /memoirs/{id}/chat   (auth)
                                     POST /memoirs/{id}/assemble  (auth)
                                     PATCH /chapters/{id}          (auth)
                                     chapters + comments (either credential)
    prompts.py                       questions: owner-side (auth) and
                                     /j/{token}/questions (link + participant)
    webhooks.py                      POST /webhooks/assemblyai    (secret header)
    dev.py                           POST /dev/signin             (gated)
```

Environment variables (`.env`): `DATABASE_URL`, `SUPABASE_URL`,
`SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `ENABLE_DEV_ROUTES`,
`ASSEMBLYAI_API_KEY`, `ASSEMBLYAI_WEBHOOK_SECRET`, `PUBLIC_BASE_URL`,
`TRANSCRIPTION_ENABLED`, `GEMINI_API_KEY`, `GEMINI_MODEL`,
`GEMINI_ASSEMBLY_MODEL`, `GROQ_API_KEY`, `AI_ENABLED`, `ALLOWED_ORIGINS`, `DB_POOL_MIN_SIZE`,
`DB_POOL_MAX_SIZE`, `DB_POOL_TIMEOUT`.

`scripts/seed_demo_memoir.py` fills a memoir with material worth assembling —
five contributors, fifteen dated memories, two accounts of one winter that
differ — then plans and assembles it. It points at whatever database `.env`
names, so it never deletes, matches contributors by name and memories by their
first line to stay idempotent, and refuses a published memoir. `--adopt-photos`
moves a stray photograph onto a memory that has words, because the planner can
only place one beside a paragraph and an archive of orphaned images assembles
with zero figures while behaving correctly.

`PUBLIC_BASE_URL` is deliberately **empty in local development** — localhost is
not reachable from the internet, so no webhook is requested and the poll path
does the work. No tunnel is needed to develop or test this.

The first three are **not** secrets — the URL and anon key ship in every
frontend bundle, and token verification uses the public JWKS. The service role
key **is** a secret, is the only one of its kind this app holds (the AssemblyAI
and Gemini keys are smaller: each can spend money on its own account and nothing
else), and exists for exactly one reason: signing upload URLs for contributors, who have no token of their own.
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
- ~~**Photographs in the PDF.**~~ Done. `export_service` fetches each figure
  with this service's own credentials, scales it to the text measure by aspect
  ratio, and prints it after the paragraph it anchors to; a carousel group
  becomes a run of plates, because paper does not advance. A photograph that
  cannot be fetched or decoded is left out with a log line and the book is still
  produced. The comment layer is still deliberately absent — it is the one part
  of a memoir still growing, and printing it freezes half a conversation.
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
  `archive/`, `contributors/`, `billing/`, `media/`, `invitation/`, `memoir/`,
  `onboarding/`. `invitation/` and `memoir/` are the two with a server data
  path, for the same reason: both are addressed by a link token rather than a
  session, so there is no browser-held credential a server render would have to
  wait for.
- `memoir/` reads the finished book at `/m/{token}`, against `GET /r/{token}`
  and the chapter routes. Its README is worth reading before changing anything
  about `block_source` here — the reader positions a photograph, a credit and a
  comment with the same arithmetic, so the offsets in this database are the
  thing that design rests on.
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
- **`src/integrations/`** — thin wrappers around external services (Postgres, Supabase Auth
  and Storage, AssemblyAI), plus the shared HTTP client they all call through. No feature
  knowledge. If the word "memoir" appears here, it's in the wrong file.
- **`src/models/`** — Pydantic request/response models, one file per feature.
- **`src/core/`** — app-wide setup: config, logging, error handlers, the lifespan that opens
  and closes the connection pool and the HTTP client.

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

- `str | None`, not `Optional[str]`.
- **`async def` route handlers, not `def`.** The whole stack is asynchronous: psycopg's
  `AsyncConnection` through the pool in `src/integrations/db.py`, and one shared
  `httpx.AsyncClient` in `src/integrations/http.py`. A plain `def` handler is run in a
  threadpool with no loop underneath it and cannot await either.
- **Never call a synchronous driver from `src/`.** No `psycopg.connect()`, no `httpx.get()`,
  no `httpx.Client()`. One of them blocks the event loop for its whole duration and with it
  every other request the worker is serving — no error, no failing test, just latency under
  load. `test_no_blocking_io_in_src` in `tests/static/test_layering.py` is what catches it.
  The two correct forms are `async with db() as conn, conn.cursor() as cur:` and
  `await (await client()).get(...)`.
- Pure helpers and FastAPI dependencies that only do CPU work (`current_user`,
  `optional_user_id`) stay `def`. FastAPI runs a sync dependency in a threadpool, which is
  right for JWT verification and wrong for anything that touches the network.
- No `__init__.py` files under `src/`, which uses implicit namespace packages. `tests/` does
  have one, so the project's `tests` package is not shadowed by an unrelated installed one.

### Config

All environment variables are read **once**, in `src/core/config.py`, into a `settings` object.
Nothing else calls `os.environ` or `load_dotenv()`. A missing variable crashes the app at startup
on purpose — better than failing on the first request.

### Database

- Borrow a connection via `db()` from `src/integrations/db.py`:
  `async with db() as conn, conn.cursor() as cur:` — then `await cur.execute(...)` and
  `await cur.fetchone()`. Leaving the block commits, or rolls back if an exception escaped,
  and returns the connection to the pool. Rows come back as `dict_row`, which is why a
  handler can return one straight to FastAPI as JSON.
- The pool is opened and closed by the lifespan in `src/core/app_lifespan.py`, sized by
  `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE`. Those are **per process**: the real ceiling on
  connections is `uvicorn --workers` x `DB_POOL_MAX_SIZE`. When the pool is empty callers
  queue, and a wait longer than `DB_POOL_TIMEOUT` becomes a 503 rather than a hang.
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
- **The test suite needs a real Postgres**, and refuses to run against one that is not named
  `memoir_test*`. It TRUNCATEs every table between tests, so pointed at the wrong database that is
  not a failing test — it is a family's recordings gone. Start one with
  `docker run --name memoir-pg -e POSTGRES_PASSWORD=postgres -p 5433:5432 -d postgres:17`.
  Without it the `db`-marked tests skip with one sentence rather than erroring, so the unit,
  static and model tiers stay useful.
- **CI runs the whole suite** on every push and PR to `main`: `.github/workflows/ci.yaml`
  starts a `postgres:17` service on 5433 and runs `pytest`. `conftest.py` does the rest — it
  sets every environment variable and applies `migrations/` itself, so the workflow carries no
  secrets and no `.env`.
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

- ~~**Nothing runs the tests but a person.**~~ Done: `.github/workflows/ci.yaml` runs the suite
  against a Postgres service container on every push and PR. The frontend has the same, running
  `npm run verify` (typecheck, lint, vitest).

  What the tiers cover, and why they are split:

  | Tier | What it proves |
  | --- | --- |
  | `security/` | 96 tests, in five files. **Read these first.** Token verification against real ES256 keys (unsigned, wrong key, wrong project, wrong audience, expired, missing `sub`, and the thirty seconds of leeway) — and that every rejection looks identical from outside. Injection: a filename never reaches a storage path, partial updates drop unknown fields, `update_memory` filters column names through a hard-coded allow-list. Leakage: no response anywhere carries a `contributor_token`, a `storage_path`, `never_forget`, or a transcript's `provider_id`. Link tokens: unknown, revoked and wrong-scope are indistinguishable, and a participant token is useless on another memoir. |
  | `api/` | 72 tests. Every endpoint at least once, answering the codes it documents — including the chapter and comment boundaries added with migration `0011` (`test_chapters.py`). Those last are really security tests wearing an `api/` label; if that file grows, split them out. |
  | `unit/` | 37 tests. Config, models, transcript payloads — no database. |
  | `db/` | 19 tests. That the CHECK constraints and partial indexes actually refuse what they claim to. |
  | `static/` | The conventions in this file, enforced by reading the source: no SQL in `api/`, no `fastapi` in `domain/`, no memoir vocabulary in `integrations/`, no route defined in `main.py`, no blanket `except Exception`, **every handler `async def`**, **no synchronous driver call anywhere under `src/`**, the service role key confined to one module, and **the API never answering 403**. |

  **`AI_ENABLED=false` for the whole suite**, set in `conftest`'s environment
  block. `Settings` reads `.env`, so without it a developer's real
  `GEMINI_API_KEY` reaches the tests and every assembly test makes a live,
  billed call — which is also how the fallback assertions used to pass for the
  wrong reason: they were relying on that call *failing*. Tests that want the
  planner path stub it on the consumer's own reference
  (`assembly_service.planner.plan`), so one that forgets to stub fails loudly
  instead of reaching Google.

  Fixtures build rows with SQL rather than by calling the API, so a test about deletion fails only
  when deletion is broken — and so it can construct states the application cannot reach, such as a
  published memoir, or a chapter, which nothing here writes yet. The suite's ancestor was
  `verify_slice3.py` in the repo root, deleted once this replaced it: it needed a live server, live
  Supabase and the **real production database** to assert the same things, and the account it
  hardcoded is still visible in that database as `slice3-verify@example.com`. A script that can only
  be run against production is not a safety net.
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
- CORS defaults to `*` when `ALLOWED_ORIGINS` is unset, which is right for a laptop and wrong
  everywhere else. Every deployed environment must set the real frontend origin.
- A contributor's `participant_token` never expires and cannot be revoked individually. Revoking
  the share link stops new contributions from everyone at once, which is the only lever there is.
