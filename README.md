# Project Backend Template

This repo is a **template/reference structure** for a FastAPI backend project. It is not a working
application — the folders and files here show you *how* to organize your code and *what kind of
content* belongs in each layer. Each file contains a minimal placeholder example (not real logic).

## Architecture

Requests flow through three layers:

```
api/  ->  domain/  ->  integrations/
(routes)   (business logic)  (external services: DB, LLM, storage, etc.)
```

- **`src/api/`** — FastAPI routers. Defines endpoints, validates input/output using models, and
  calls into `domain/` for the actual work. Should contain little to no business logic itself.
- **`src/domain/`** — Business logic, organized by feature (one subfolder per feature). Each
  feature folder can have multiple files: the main logic, a `constants.py`, and a `utils.py`.
- **`src/models/`** — Pydantic models (request/response schemas), one file per feature, matching
  the names used in `api/`.
- **`src/integrations/`** — Thin wrappers around external services (databases, LLM providers,
  cloud storage, etc.). Nothing feature-specific lives here.
- **`src/core/`** — App-wide setup: lifespan/startup hooks, logging configuration.
- **`src/utils/`** — Small generic helpers not tied to any one feature.

## Folder structure

```
src/
  main.py                 # FastAPI app entrypoint, registers routers
  api/
    example.py            # Example router (1 feature shown)
  core/
    app_lifespan.py        # Startup/shutdown hooks
    logging_config.py      # Logger setup
  domain/
    example_feature/
      example_service.py   # Main business logic for the feature
      constants.py          # Feature-specific constants
      utils.py              # Feature-specific helper functions
  models/
    example_models.py       # Request/response schemas for the example feature
  integrations/
    llm_client.py            # Example external service wrapper (LLM)
  utils/
    string_helpers.py        # Generic reusable helper (not feature specific)
```

## How to use this template

1. Copy this structure for your own project.
2. For each new feature you build, add:
   - one file in `src/api/`
   - one folder in `src/domain/<feature_name>/`
   - one file in `src/models/`
3. Keep routes thin, keep business logic in `domain/`, and keep external service calls isolated in
   `integrations/`.

## Setup

```bash
pip install -r requirements.txt
uvicorn src.main:app --reload
```
