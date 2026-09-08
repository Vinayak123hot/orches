"""Azure AI Foundry client -- one per worker process, with a real lifecycle.

    warm()   build it at startup      (main.py lifespan, before serving)
    openai   use it                   (services and agents)
    close()  release it at shutdown   (main.py lifespan, after serving)

WHY IT IS STILL LAZY UNDERNEATH. warm() is an optimisation, not the contract: `openai`
builds the client on first use if nothing warmed it. That matters because a brief Entra
outage at boot must not stop the app from starting -- if it did, the platform would
restart the app, hit the same failure, and take /health down with it. So a failed
warm-up is logged and the next request tries again.

It also has to be thread-safe: 40 request threads share this object, so without the Lock
several of them could each build a client on a cold start. `_openai_client` is assigned
LAST, so a failed build leaves the object un-initialised and is retried rather than
cached half-built.

This replaced a module-level `state` dict plus a `_state_ready` flag and a `global`
statement -- an object written without a class, where two threads could enter at once and
a failed init left the flag set.
"""
import threading

from app.core.config import logger
from app.core.errors import UpstreamUnavailable


class FoundryClient:
    """Lazily builds and caches the AIProjectClient for this worker process."""

    def __init__(self, endpoint: str, timeout: int = 20, max_retries: int = 1):
        self._endpoint = endpoint
        # Bounds ONE request; see Settings.FOUNDRY_HTTP_TIMEOUT for the worst-case math.
        self._timeout = timeout
        self._max_retries = max_retries
        # Both are filled in by _ensure() -- nothing is built here, so CONSTRUCTING this
        # object costs nothing and touches no network. That is what lets deps.py create
        # it at import time.
        #   _openai_client MUST start as None: _ensure() reads it as its
        #                  "have I built this already?" check.
        #   _project_client is held for two reasons: it keeps the client that produced
        #                  the OpenAI client alive for the life of the process, and
        #                  close() needs it to release the connection pool.
        self._project_client = None
        self._openai_client = None
        self._lock = threading.Lock()

    @property
    def openai(self):
        """The OpenAI-compatible client, building it on first use."""
        self._ensure()
        return self._openai_client

    def warm(self) -> None:
        """Build the client NOW, so the first real request doesn't pay for it.

        Called once at startup from the lifespan. NEVER RAISES: a warm-up failure is
        logged and left to the lazy path, because the app must be able to start while
        Azure is briefly unreachable -- see the module docstring.

        Blocking (it performs the Entra handshake), which is fine where it is called:
        the lifespan runs before uvicorn accepts any connection, so there is no request
        and no event-loop work to hold up.
        """
        try:
            self._ensure()
        except Exception as exc:
            logger.warning(
                "Foundry warm-up failed (%s: %s); the first request will retry",
                type(exc).__name__, exc,
            )

    def close(self) -> None:
        """Release the client's HTTP transport at shutdown. Best-effort.

        The AIProjectClient holds a connection pool; closing it lets a graceful shutdown
        finish without leaving sockets to be reclaimed by process death.

        The references are cleared FIRST, so a request that somehow arrives during
        shutdown rebuilds a fresh client instead of using a closed one -- `openai` would
        otherwise hand back a client whose transport is gone.
        """
        project_client, self._project_client = self._project_client, None
        self._openai_client = None
        if project_client is None:
            return
        try:
            project_client.close()
            logger.info("Foundry client closed")
        except Exception:
            logger.exception("could not close the Foundry client")

    def _ensure(self) -> None:
        """Build the client once per process, under a lock.

        Double-checked locking: the fast path is a plain attribute read with no lock,
        and only the first callers serialise. `_openai_client` is assigned last, so a
        failed build leaves the object un-initialised and the next request retries
        instead of finding a half-built client cached.
        """
        #A process starts. The first request in builds the Foundry client while a couple of others briefly wait on the lock; every request after that reuses it with no waiting
        if self._openai_client is not None:
            return
        with self._lock:
            if self._openai_client is not None:
                return
            # Imported here rather than at module scope so importing this module (and
            # therefore the app) doesn't require the Azure SDKs to be installed.
            from azure.ai.projects import AIProjectClient
            from azure.identity import DefaultAzureCredential

            project_client = AIProjectClient(
                credential=DefaultAzureCredential(),
                endpoint=self._endpoint,
                # azure-core transport options, for AIProjectClient's own calls.
                connection_timeout=self._timeout,
                read_timeout=self._timeout,
            )
            # get_openai_client() passes its keyword arguments straight to the OpenAI
            # client constructor, so this is what actually bounds conversations.create()
            # -- the first network call of every new chat. max_retries is set explicitly
            # because the SDK defaults to 2, which would silently triple the ceiling.
            openai_client = project_client.get_openai_client(
                timeout=self._timeout, max_retries=self._max_retries
            )
            self._project_client = project_client
            self._openai_client = openai_client
            logger.info(
                "Foundry client initialized (lazy) timeout=%ss max_retries=%s",
                self._timeout, self._max_retries,
            )

    def create_conversation(self):
        """Create a new, empty Foundry conversation.

        Every failure below this line becomes UpstreamUnavailable, so this client raises
        the same domain error the agents raise instead of leaking SDK types (openai's
        APITimeoutError, azure-core's ServiceRequestError, azure-identity's
        ClientAuthenticationError). Two reasons that matters:

          * the log line names the cause -- callers used to see only a generic
            "chat failed" with a raw traceback;
          * nothing above this layer has to import openai or azure.core to reason about
            a failure, and a non-HTTP caller (the job reaper, a script) gets an error it
            can recognise.

        The chat controller still turns this into the user-facing fallback bubble; it
        catches Exception, and UpstreamUnavailable is one.
        """
        try:
            return self.openai.conversations.create()
        except Exception as exc:
            # Includes the lazy build in _ensure(), which runs on the first call.
            logger.error(
                "foundry create_conversation failed (%s): %s", type(exc).__name__, exc
            )
            raise UpstreamUnavailable("Foundry conversation could not be created")
