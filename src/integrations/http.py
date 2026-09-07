# Integrations = thin wrappers around external services. This one is not a
# service at all — it is the HTTP client the other wrappers talk through.
#
# ---------------------------------------------------------------------------
# Why one shared client rather than a fresh one per call
#
# `httpx.get(...)` builds a client, opens a TCP connection, negotiates TLS,
# sends one request and throws the whole thing away. Against Supabase Storage,
# which this app calls several times to render a single page, that is a full
# handshake per signed URL.
#
# One long-lived AsyncClient keeps a pool of connections open and reuses them,
# so the second call to the same host costs a round-trip instead of a
# handshake. It is also the only way to put a global timeout and a connection
# ceiling somewhere a caller cannot forget them.
#
# The other half of the reason is async. A synchronous httpx call inside an
# `async def` handler blocks the event loop exactly as hard as a synchronous
# database call does; every wrapper in this package awaits instead.
# ---------------------------------------------------------------------------

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def open_client() -> httpx.AsyncClient:
    """Build the shared client. Idempotent; called by the app lifespan."""
    global _client
    async with _client_lock:
        if _client is not None:
            return _client
        _client = httpx.AsyncClient(
            # A timeout on every request, set once, that no call site can
            # forget. Without it a hung upstream holds a connection — and the
            # request waiting on it — indefinitely.
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
        logger.info("Shared HTTP client open")
        return _client


async def close_client() -> None:
    """Close the shared client and its connections. Called on shutdown."""
    global _client
    async with _client_lock:
        if _client is None:
            return
        await _client.aclose()
        _client = None
        logger.info("Shared HTTP client closed")


async def client() -> httpx.AsyncClient:
    """The shared client, opening it on first use if the lifespan has not run.

    The lazy open is for tests and one-off scripts that call an integration
    directly. In the server the lifespan has always got here first.
    """
    return _client or await open_client()
