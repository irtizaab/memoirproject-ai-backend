# The entrypoint. This file is wiring and nothing else.
#
# If you ever find yourself writing a route, a SQL query or a pydantic model in
# here, it belongs somewhere else — see AGENTS.md. Everything below is either
# "set the app up" or "attach a piece that lives in another file".
#
# Run it with:  uvicorn src.main:app --reload

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.accounts import router as accounts_router
from src.api.drafts import router as drafts_router
from src.api.health import router as health_router
from src.api.links import router as links_router
from src.api.memoirs import router as memoirs_router
from src.core.app_lifespan import lifespan
from src.core.config import settings
from src.core.error_handlers import register_error_handlers
from src.core.logging_config import setup_logging

# Called before the app is created so that anything logging during startup
# already has the formatter applied.
setup_logging()

# `lifespan` is a startup/shutdown hook: the code before its `yield` runs once
# when the server boots, the code after it runs once when the server stops.
# It's the right place for anything that needs setting up and tearing down,
# like a connection pool later on.
app = FastAPI(title="The Memoir Project API", lifespan=lifespan)

# CORS — Cross-Origin Resource Sharing. Browsers refuse to let a page on one
# origin call an API on another unless the API says it's allowed. The frontend
# runs on localhost:3000 and this runs on localhost:8000, which count as
# different origins, so without this every request from the browser fails
# before it reaches any of the code below.
#
# TODO: allow_origins=["*"] is fine for local development and wrong for
# production. Replace it with the real frontend origin before deploying.
# It is tolerable today only because auth is a header token, not a cookie.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Turn Postgres constraint violations into 400/409 responses instead of 500s.
register_error_handlers(app)

# Each router carries its own paths and prefix; registering it here is what
# puts those routes on the app. A new feature = a new file in src/api/ and one
# more line here.
app.include_router(health_router)      # GET   /health
app.include_router(drafts_router)      # POST  /drafts, PATCH /drafts/{id}
app.include_router(memoirs_router)     # POST  /memoirs/claim         (auth)
app.include_router(accounts_router)    # GET   /me                    (auth)
app.include_router(links_router)       # GET   /j/{token}             (public)

# Development-only routes, registered only when explicitly switched on.
#
# The import sits inside the `if` on purpose. Off by default means the module
# is never even loaded, so the route cannot appear in /docs or be reached by
# guessing the path. There is no runtime branch inside a handler to get wrong.
if settings.enable_dev_routes:
    from src.api.dev import router as dev_router

    app.include_router(dev_router)     # POST  /dev/signin
