# The Memoir Project — API

FastAPI + Postgres. Async end to end: psycopg's `AsyncConnection` through a connection pool,
and one shared `httpx.AsyncClient` for Supabase Storage and AssemblyAI.

**`AGENTS.md` is the documentation.** It covers the architecture, the layering rules, what
every file is for, the security boundaries, and the known limitations. This file only tells
you how to run the thing.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill it in — see the variable list in AGENTS.md
python migrate.py             # apply migrations/*.sql in order
uvicorn src.main:app --reload
```

`GET /health` opens a pooled connection and asks Postgres for a row, so a 200 there means the
API is up *and* the database is reachable. Interactive docs are at `/docs`.

## Test it

The suite needs a real Postgres. It TRUNCATEs every table it can reach, so it refuses to run
against a database not named `memoir_test*`.

```bash
pip install -r requirements-dev.txt
docker run -d --name memoir-test-db -p 5433:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=memoir_test postgres:16
python -m pytest
```

Point it elsewhere with `TEST_DATABASE_URL`. Start with `tests/security/` — those are the
boundaries the product promises to hold.

## Deploy it

```bash
docker build -t memoir-api .
```

Runs `uvicorn --workers 4` as a non-root user, with a `HEALTHCHECK` against `/health`.

Two settings matter before this faces traffic:

- **`ALLOWED_ORIGINS`** — defaults to `*`, which is right for a laptop and wrong for
  production. Set it to the real frontend origin.
- **`DB_POOL_MAX_SIZE`** — per *process*. Total connections against Postgres is
  `workers x DB_POOL_MAX_SIZE`, so four workers at the default 20 hold 80. Size it against
  what the database or its pooler actually allows, remembering that a rolling deploy runs
  old and new containers at once.
