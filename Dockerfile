# The production image.
#
# Two things shape it: the app is async (so one process serves many requests
# concurrently, and several worker processes serve many more), and the only
# secret it holds is passed in at runtime, never baked in.

FROM python:3.11-slim

# Never write .pyc files into the image, and never buffer stdout — an unflushed
# log line is a log line you do not have when the container dies.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Dependencies first, as their own layer. Application code changes on every
# commit; requirements.txt does not, so this layer stays cached and a rebuild
# does not re-download the world.
#
# requirements.txt only — requirements-dev.txt is deliberately absent, so no
# test framework and no HTTP mocking library ship to a server.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY migrations ./migrations
COPY migrate.py ./

# Run as a user with no privileges and no ownership of the code it executes.
# A process that cannot write to /app cannot be talked into rewriting its own
# source, which is the point.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# The same liveness check the app exposes to everything else — it opens a
# connection from the pool and asks Postgres for a row, so a container that
# cannot reach the database is reported unhealthy rather than quietly serving
# 500s. `python` rather than curl because the slim image has no curl and
# adding one would be a package installed to make one HTTP request.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# --workers 4: one Python process is one GIL, so async concurrency inside a
# worker still only uses one core. Four processes use four.
#
# Mind the arithmetic with the pool: total connections against Postgres is
# workers x DB_POOL_MAX_SIZE (default 20), so this container alone can hold 80.
# Raise the worker count and you must lower DB_POOL_MAX_SIZE, or a rolling
# deploy will exhaust the database while old and new containers overlap.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
