"""FastAPI application: lifespan, CORS, routing and domain-error mapping.

Exposes `app`, which is what gunicorn serves:

    gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker   (see startup.sh)

This module only wires things together -- configuration lives in core/config.py,
endpoints in api/routes/, business logic in services/ and domain/.

THE LIFESPAN ACTUALLY RUNS NOW. Under Azure Functions it never did (the ASGI app was
mounted per invocation), which is why the Foundry client is built lazily on first use and
why the job reaper had to be a platform timer. On App Service we get real startup and
shutdown hooks, so the reaper is a task owned by this app -- started here, cancelled here.
The lazy Foundry build stays: it is still the right behaviour, because a transient Entra
outage should fail one request rather than prevent the app from starting at all.
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.core.config import logger, settings
from app.core.errors import (
    AppError,
    ConversationEnded,
    DatabaseUnavailable,
    NotFound,
    UpstreamUnavailable,
)
# The process-wide Foundry client, so the lifespan can build it at startup and release
# it at shutdown. deps.py owns its construction; this module only owns its lifecycle.
from app.deps import foundry_client
from app.workers import reaper


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Everything that happens once at startup, and once at shutdown.

        startup   check the config, build the Foundry client, start the job reaper
        shutdown  cancel the reaper, close the Foundry client

    The config check runs here rather than at import so it is logged once per worker
    process at a point that appears in the log stream, instead of during module import
    where the platform may swallow it.

    ORDER MATTERS on the way out: the reaper is stopped BEFORE the Foundry client is
    closed, because a sweep calls agents that use that client. Closing first would leave
    a sweep talking to a dead transport.
    """
    settings.warn_on_risky_config()

    # Blocking, and deliberately so: uvicorn accepts no connection until this coroutine
    # reaches `yield`, so there is no request and no event-loop work to hold up. It never
    # raises -- see FoundryClient.warm -- so a Foundry outage delays the first agent call
    # instead of preventing the app from starting.
    foundry_client.warm()

    reaper_task = None
    if settings.REAPER_ENABLED:
        # create_task, not await: run_forever() never returns, so awaiting it here would
        # mean the app never finishes starting. The handle is kept for cancel() below --
        # a task nothing references can also be garbage-collected mid-run.
        reaper_task = asyncio.create_task(
            reaper.run_forever(settings.REAPER_INTERVAL_SECONDS), name="job-reaper"
        )
        logger.info(
            "job reaper started: sweeping every %ss (needs Always On to keep running)",
            settings.REAPER_INTERVAL_SECONDS,
        )
    try:
        yield
    finally:
        if reaper_task is not None:
            reaper_task.cancel()
            # return_exceptions so the expected CancelledError does not surface as an
            # error during a normal shutdown. This waits for the CANCELLATION, not for
            # any thread the task handed work to -- a thread cannot be cancelled, so an
            # in-flight sweep finishes on its own. That is safe: every write is inside a
            # transaction, and claiming a job is a compare-and-swap.
            await asyncio.gather(reaper_task, return_exceptions=True)
            logger.info("job reaper stopped")
        foundry_client.close()


app = FastAPI(title="IT Support Orchestrator API", lifespan=lifespan)

# Credentials require explicit origins (never "*") per browser rules and Snyk; if
# CORS_ORIGINS is ever set to "*", drop credentials rather than ship the invalid,
# insecure "*" + credentials combination.
_allow_credentials = "*" not in settings.CORS_ORIGINS
if not _allow_credentials:
    logger.warning("CORS_ORIGINS contains '*'; disabling allow_credentials")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=_allow_credentials,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health", include_in_schema=False)
def health():
    """Liveness probe, for the App Service health check and any load balancer.

    Deliberately OUTSIDE the API prefix, so the probe path does not move if
    API_PREFIX changes.

    Deliberately does NOT touch the database or any agent. A probe runs every few
    seconds from every instance: if it opened a SQL connection it would consume pool
    capacity that real requests need, and a brief SQL blip would make the platform
    recycle healthy instances -- turning a small outage into a large one. This answers
    "is this process serving?", which is the question the platform is asking.
    """
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Domain error -> HTTP status. The ONE place that translation happens, so the
# layers below (services/, domain/, db/, agents/) can raise the framework-free
# errors in core/errors.py and stay callable without a web server.
#
# The detail text of NotFound/ConversationEnded is author-written and safe to
# show; DatabaseUnavailable/UpstreamUnavailable carry internal causes, so those
# are logged and replaced with a generic line.
#
# A handler only fires if nothing caught the exception on the way up -- the
# controllers in api/routes/ deliberately re-raise NotFound/ConversationEnded so
# they reach here, and swallow everything else into a user-facing fallback.
#
# Starlette matches the CLOSEST registered class, walking up the hierarchy. So the
# four specific handlers below win over the AppError catch-all at the end.
# ---------------------------------------------------------------------------
@app.exception_handler(NotFound)
async def _handle_not_found(request: Request, exc: NotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ConversationEnded)
async def _handle_conversation_ended(request: Request, exc: ConversationEnded):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(UpstreamUnavailable)
async def _handle_upstream_unavailable(request: Request, exc: UpstreamUnavailable):
    logger.error("upstream unavailable path=%s: %s", request.url.path, exc)
    return JSONResponse(status_code=502, content={"detail": "Agent call failed"})


@app.exception_handler(DatabaseUnavailable)
async def _handle_database_unavailable(request: Request, exc: DatabaseUnavailable):
    logger.error("database unavailable path=%s: %s", request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


@app.exception_handler(AppError)
async def _handle_unmapped_domain_error(request: Request, exc: AppError):
    """Last resort: a domain error with no handler of its own.

    The four handlers above take priority, so reaching here means a subclass was added
    to core/errors.py and never mapped. The status stays 500 because we genuinely don't
    know what an unmapped error means -- inventing a 4xx would be worse. The value is the
    log line: it names the type, instead of leaving an anonymous 500 to be guessed at.
    """
    logger.error(
        "unmapped domain error %s on %s: %s",
        type(exc).__name__, request.url.path, exc,
    )
    return JSONResponse(status_code=500, content={"detail": "Internal error"})


# Every route lives under the prefix (default /api), so the endpoints are /api/chat,
# /api/jobs/status and so on. The frontend's VITE_API_URL must include it. /health above
# is outside it on purpose, so a platform probe survives a prefix change.
app.include_router(router, prefix=settings.API_PREFIX)
