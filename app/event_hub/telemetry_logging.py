####################################################################################################
# Project name      : IT Support Orchestrator API -- Azure App Service (FastAPI)                   #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# Structured JSON logging on a stable schema, with an optional Azure Event Hub emitter.            #
#   1. StructuredLogger: one JSON line per event, always to stdout, optionally to Event Hub.       #
#   2. EventHubLogEmitter: BUFFERED forwarding, so a slow Event Hub is never a slow request.       #
#   3. LogFactory: hand out per-component loggers that share one level and one emitter.            #
#   4. build_log_factory: read Settings and return a factory that is OFF unless fully configured.  #
#                                                                                                  #
# Source:-                                                                                         #
#   - json serialises each record; logging routes it to stdout at the matching stdlib level.       #
#   - datetime (UTC) stamps every record; typing supplies the Any / Optional hints.                #
#   - azure.eventhub + azure.identity are imported LAZILY, so an environment that never enables    #
#       forwarding does not need the packages installed at all.                                    #
####################################################################################################
"""Structured JSON logging, with an optional Azure Event Hub emitter for Splunk.

What this module is:
    - Four pieces: StructuredLogger (emit one record), EventHubLogEmitter (forward it),
      LogFactory (hand out loggers) and build_log_factory (wire it from Settings).

Why this exists:
    - Adapted from event_hub/telemetry_logging.py at the project root. The interface is
      unchanged -- LogFactory / get_logger(component) / log(event, correlation_id, level,
      **fields) -- so anything written against that standard works here untouched. Four
      things differ, each noted where it happens:

        1. NO logging.basicConfig here. app/core/config.py configures root logging at
           import, and basicConfig does nothing once a handler exists -- so a second call
           was a silent no-op whose level and stream were quietly ignored. One owner, and
           it is the module that always loads first.
        2. correlation_id is our conversation_id. It ties every line of a conversation
           together -- the flow, each agent, ServiceNow, the DB layer -- which is the whole
           point of the field and the thing that is unanswerable without it.
        3. log(..., exc_info=True) writes the traceback to STDOUT ONLY. Event Hub still
           gets the clean JSON with error_type. We keep the stack for our own bugs; Splunk
           gets the exception class and nothing that might carry a path, an address or a
           key.
        4. The level threshold is the stdlib's, not a parallel one, so LOG_LEVEL is the
           only place that decides what is emitted.

Security and production notes:
    1. WHAT MUST NEVER BE IN A FIELD: user emails, ServiceNow-bound free text, raw device
       output (job_output), raw agent responses, exception MESSAGES, or a URL with ?code=
       in it. These records leave the tenant. Pass error_type, ids and numbers.
    2. Event Hub auth is Managed Identity via DefaultAzureCredential -- no keys, no
       connection strings, nothing to rotate.
    3. A logging sink must never break business flow: every emit failure is caught, and
       Event Hub's own failures are reported through the plain stdlib logger to avoid a
       log-about-the-log loop.
"""

# ============================================ Imports =============================================
from __future__ import annotations  # Postponed annotation evaluation (PEP 563) for forward hints    # future

import json  # Serialise each record into the single JSON line that is emitted                       # stdlib json
import logging  # Route records to stdout at the matching stdlib level                               # stdlib logging
from datetime import datetime, timezone  # UTC timestamp stamped on every record                     # stdlib datetime
from typing import Any, Optional  # Any for arbitrary fields, Optional for the emitter               # stdlib typing

# =========================================== Constants ============================================
# The stdlib's own numbers, so `level` here and LOG_LEVEL there mean the same thing.
_LEVEL_ORDER: dict[str, int] = {
    "DEBUG": logging.DEBUG,  # Developer detail; not normally emitted in production                  # level
    "INFO": logging.INFO,  # The default: routine, countable events                                  # level
    "WARNING": logging.WARNING,  # Degraded but handled, e.g. a skipped record                       # level
    "ERROR": logging.ERROR,  # A failure a human should look at                                      # level
    "CRITICAL": logging.CRITICAL,  # Highest severity; unused by this app today                      # level
}

# Event Hub's OWN failures must never be reported through a StructuredLogger -- see
# EventHubLogEmitter._on_send_error. This is the plain stdlib logger they go to instead.
_fallback_logger = logging.getLogger(__name__)  # Plain logger, breaks the log-about-log loop        # fallback


# ======================================= Event Hub emitter ========================================
class EventHubLogEmitter:
    """Forward each structured log line to Azure Event Hub (for Splunk).

    What this class is:
        - A thin wrapper over EventHubProducerClient in BUFFERED mode: emit() appends to an
          in-process buffer and returns; the SDK's own background thread batches and sends.

    Why it exists:
        - BUFFERED MODE is the whole point of this class. The version before this one sent
          one AMQP round trip per log line ON THE CALLING THREAD, so ten lines cost ten
          sequential sends -- ~100ms at the service's average and ~300ms at its p99, added
          to every request, holding a threadpool worker the whole time. A slow Event Hub
          was a slow chat. Now it is neither.

    Security and production notes:
        1. Auth is Managed Identity via DefaultAzureCredential -- no keys, no connection
           strings, nothing to rotate.
        2. WHAT IS LOST IF THE PROCESS IS KILLED: whatever is still buffered. Accepted,
           because these are log lines. close() flushes, so an orderly shutdown loses
           nothing; a SIGKILL loses up to max_wait_time's worth.
        3. The azure-eventhub SDK is imported lazily, so an environment that does not
           enable Event Hub never needs the package installed.

    Example:
        >>> emitter = EventHubLogEmitter("ns.servicebus.windows.net", "logs")  # doctest: +SKIP
    """

    def __init__(
        self,
        fully_qualified_namespace: str,
        event_hub_name: str,
        max_wait_time: float = 5.0,
        max_buffer_length: int = 1500,
        enqueue_timeout: float = 1.0,
    ) -> None:
        """Open the buffered producer and the managed-identity credential.

        What this method does:
            - Lazily imports the SDKs, builds a DefaultAzureCredential, then constructs an
              EventHubProducerClient in buffered mode with explicit batching limits.

        Why it exists:
            - Every batching knob is set explicitly rather than left to the SDK default, so
              the memory ceiling and the thread count inside a gunicorn worker are both
              visible in the code.

        Security and production notes:
            1. DefaultAzureCredential means production uses the App Service managed
               identity; that identity needs the Event Hubs Data Sender role.
            2. Constructing this opens a connection, so it happens once per process (in
               build_log_factory) and is released by close() in the lifespan teardown.

        Args:
            fully_qualified_namespace: e.g. my-namespace.servicebus.windows.net.
            event_hub_name: The hub (topic) records are sent to.
            max_wait_time: Flush at least this often, in seconds, even when nearly empty.
            max_buffer_length: Buffered events PER PARTITION before enqueue blocks.
            enqueue_timeout: Seconds emit() will wait on a full buffer before raising.

        Returns:
            None.
        """
        from azure.eventhub import EventHubProducerClient  # Lazy: only needed when enabled          # sdk import
        from azure.identity import DefaultAzureCredential  # Managed identity, no secrets            # sdk import

        self._enqueue_timeout = enqueue_timeout  # Backstop for a FULL buffer; see emit()            # enqueue to
        self._credential = DefaultAzureCredential()  # Released by close(), alongside the producer   # credential
        self._producer = EventHubProducerClient(
            fully_qualified_namespace=fully_qualified_namespace,  # ns.servicebus.windows.net        # namespace
            eventhub_name=event_hub_name,  # The hub records are published to                        # hub name
            credential=self._credential,  # Managed identity; nothing to rotate                      # credential
            buffered_mode=True,  # THE point of this class: emit() never does a round trip           # buffered
            # Flush at least this often even when the buffer is nearly empty. The SDK's
            # default is 1s; 5s trades a little Splunk latency for fewer, fuller batches.
            max_wait_time=max_wait_time,  # Flush cadence, in seconds                                # flush secs
            # Per PARTITION, not in total -- a four-partition hub buffers up to 4x this.
            max_buffer_length=max_buffer_length,  # Memory ceiling, per partition                    # buffer len
            # One sender thread is ample for log volume and keeps the thread count in a
            # gunicorn worker predictable. Omitting this gets a default ThreadPoolExecutor
            # of min(32, cpu_count + 4) workers, which is a lot of threads to ship logs.
            buffer_concurrency=1,  # Exactly one sender thread per worker process                    # threads
            on_success=self._on_send_success,  # Called on the SDK thread when a batch lands         # ok hook
            on_error=self._on_send_error,  # Called on the SDK thread when a batch fails             # err hook
        )

    # ========================================= SDK callbacks ======================================
    @staticmethod  # No instance state needed; the SDK calls this on its own thread
    def _on_send_success(events, partition_id) -> None:
        """Called on the SDK's thread once a batch lands. Nothing to do."""

    @staticmethod  # No instance state needed; the SDK calls this on its own thread
    def _on_send_error(events, partition_id, error) -> None:
        """Called on the SDK's thread when a batch could not be sent.

        What this method does:
            - Logs the dropped record count and the error TYPE through the plain stdlib
              logger, and does nothing else.

        Why it exists:
            - Deliberately uses the stdlib logger. Reporting this through a
              StructuredLogger would write a log line, which would enqueue another event,
              which could fail the same way -- a loop that ends in a full buffer or a blown
              stack.

        Security and production notes:
            1. error_type only, never str(error): these carry the namespace and the host.

        Args:
            events: The batch that could not be sent; only its length is used.
            partition_id: The hub partition the batch was destined for.
            error: The SDK exception; only its class name is logged.

        Returns:
            None.
        """
        _fallback_logger.error(  # Plain stdlib logger, so this cannot re-enter the emitter          # log error
            "Event Hub send failed, %d record(s) dropped: %s",
            len(events), type(error).__name__,  # Count and CLASS only -- never str(error)           # type only
        )

    # ============================================ Public API ======================================
    def emit(self, record_json: str) -> None:
        """Enqueue one JSON log line. Returns as soon as it is buffered.

        What this method does:
            - Wraps the line in an EventData and hands it to the buffered producer with a
              bounded enqueue timeout.

        Why it exists:
            - send_event, not send_batch: in buffered mode a batch handed in is kept intact
              and sent as one unit, so batching here would fight the SDK's own batching.

        Security and production notes:
            1. The timeout is the backstop for a FULL buffer, which is what a long Event Hub
               outage looks like from in here. Without it this call blocks in request
               handling -- the exact thing buffered mode exists to stop. On timeout it
               raises, and StructuredLogger.log already swallows that.

        Args:
            record_json: One already-serialised JSON record.

        Returns:
            None.

        Raises:
            Exception: If the buffer is full and the enqueue timeout expires.
        """
        from azure.eventhub import EventData  # Lazy, for the same reason as the constructor         # sdk import

        self._producer.send_event(  # Buffered: appends and returns, no AMQP round trip here         # enqueue
            EventData(record_json), timeout=self._enqueue_timeout  # Bounded wait on a full buffer   # bounded
        )

    def close(self) -> None:
        """Flush what is buffered, then release the producer and the credential.

        What this method does:
            - Closes the producer (which flushes by default), then closes the credential in
              a `finally` so it is released even if the flush fails.

        Why it exists:
            - Called from LogFactory.close() in the lifespan teardown, so an orderly
              shutdown loses no buffered lines.

        Security and production notes:
            1. The credential close is in a `finally`: a failed flush must not leak the
               token cache and its background refresh thread.

        Returns:
            None.
        """
        try:  # Flushing may fail if the hub is unreachable at shutdown                              # close try
            self._producer.close()  # flush=True is the default                                      # flush+close
        finally:  # Release the credential either way, so no refresh thread is left behind           # always
            self._credential.close()  # Drops the token cache and its refresh timer                  # close cred


# ======================================= Structured logger ========================================
class StructuredLogger:
    """Emit structured JSON events on a stable schema.

    What this class is:
        - A per-component logger. Every record carries timestamp / event / correlation_id /
          agent_name / level, plus whatever fields the caller adds. It goes to stdout as one
          JSON line and, when an emitter is configured, to Event Hub.

    Why it exists:
        - A fixed schema is what makes the lines queryable. `agent_name` keeps its original
          name even though components include the flow and the services as well as the
          agents -- Splunk queries and dashboards key on it, and matching the existing field
          matters more than a tidier word.

    Security and production notes:
        1. The **fields the caller passes are written verbatim, so the PII rule in the
           module docstring is enforced by the CALLER, not here. Pass ids and numbers.
        2. An Event Hub emit failure is caught and logged; it can never propagate into the
           business flow that was doing the logging.

    Example:
        >>> logger.log(event="turn_completed", correlation_id="c1", duration_ms=3204)  # doctest: +SKIP
    """

    def __init__(
        self,
        component_name: str,
        min_level: str,
        emitter: Optional[EventHubLogEmitter] = None,
    ) -> None:
        """Bind this logger to one component name, one level threshold and one emitter.

        Args:
            component_name: Written as `agent_name`; also the stdlib logger's name.
            min_level: Level name below which records are dropped; unknown names -> INFO.
            emitter: Shared Event Hub emitter, or None when forwarding is off.

        Returns:
            None.
        """
        self._component_name = component_name  # Emitted as `agent_name` on every record             # component
        self._min_level = _LEVEL_ORDER.get(min_level.upper(), logging.INFO)  # Unknown -> INFO       # threshold
        self._emitter = emitter  # None when forwarding is off, which is the default                 # emitter
        self._python_logger = logging.getLogger(component_name)  # Root config is core/config's      # stdlib log

    def log(
        self,
        event: str,
        correlation_id: str,
        level: str = "INFO",
        exc_info: bool = False,
        **fields: Any,
    ) -> None:
        """Emit one structured event.

        What this method does:
            - Drops the record if it is below the threshold; otherwise builds the fixed
              schema, merges the caller's fields, serialises once, writes to stdout and
              (when configured) enqueues the same JSON for Event Hub.

        Why it exists:
            - One call site for both sinks, so stdout and Splunk can never disagree about
              what happened. The JSON is serialised ONCE and reused for both.

        Security and production notes:
            1. `exc_info=True` attaches the traceback to the STDOUT record only -- Event Hub
               receives the clean JSON. Pass error_type as a field alongside it.
            2. The Event Hub enqueue is wrapped in try/except: a logging sink must never
               break business flow. In buffered mode what reaches here is a full buffer
               (enqueue timed out), not a failed send -- a send that fails does so later, on
               the SDK's thread, and is reported by the emitter's own on_error.

        Args:
            event: snake_case NAME, e.g. "turn_completed". A name, not a sentence: it is
                the thing you count, and a reworded sentence breaks every saved query.
            correlation_id: The conversation_id, so one filter returns the whole conversation.
            level: Severity name; anything unrecognised is treated as INFO.
            exc_info: True inside an `except` block, to attach the traceback to stdout.
            **fields: ids, numbers, enums. Never free text, never PII -- see the module
                docstring.

        Returns:
            None.

        Example:
            >>> logger.log(event="job_started", correlation_id="c1", job_kind="diagnostic")  # doctest: +SKIP
        """
        if _LEVEL_ORDER.get(level, logging.INFO) < self._min_level:  # Below LOG_LEVEL -> drop       # too low?
            return  # One threshold, the stdlib's, so LOG_LEVEL is the only decider                  # skip record

        record: dict[str, Any] = {  # The FIXED schema every consumer keys on                        # build record
            "timestamp": datetime.now(timezone.utc).isoformat(),  # UTC, ISO-8601                    # timestamp
            "event": event,  # snake_case name -- the thing dashboards count                         # event
            "correlation_id": correlation_id,  # Our conversation_id, ties the turn together         # correlation
            "agent_name": self._component_name,  # Kept as agent_name: Splunk queries key on it      # component
            "level": level,  # Severity as a name, matching the stdlib vocabulary                    # level
        }
        record.update(fields)  # Caller's ids/numbers; the PII rule is enforced by the caller        # add fields
        record_json = json.dumps(record, ensure_ascii=False, default=str)  # Serialise ONCE, reused below  # to json

        # stdout: the JSON line, plus the traceback when asked for.
        self._python_logger.log(  # format="%(message)s" keeps stdout valid JSON                     # to stdout
            _LEVEL_ORDER.get(level, logging.INFO), record_json, exc_info=exc_info  # Traceback: stdout only  # level+trace
        )

        if self._emitter is not None:  # Forwarding is off by default, so usually skipped            # forwarding?
            # A logging sink must never break business flow. In buffered mode what
            # reaches here is a full buffer (enqueue timed out), not a failed send --
            # a send that fails does so later, on the SDK's thread, and is reported by
            # the emitter's own on_error.
            try:  # Enqueue the SAME JSON that went to stdout, so the two cannot disagree            # emit try
                self._emitter.emit(record_json)  # Buffered append; returns immediately              # enqueue
            except Exception:  # Full buffer -- log it and carry on with the request                 # emit failed
                self._python_logger.exception("Failed to emit log to Event Hub.")  # Never re-raise  # log drop


# ========================================== Log factory ===========================================
class LogFactory:
    """Hands out StructuredLoggers that share one level and one emitter.

    What this class is:
        - A tiny factory holding the process-wide level and the single Event Hub emitter,
          so every component logger agrees on both.

    Why it exists:
        - Does NOT configure root logging -- app/core/config.py owns that (reason 1 in the
          module docstring). Build one factory per process in deps.py and close it in the
          lifespan teardown, so the Event Hub producer is opened and released once.

    Security and production notes:
        1. The emitter is SHARED by every logger this factory hands out, which is what keeps
           it to one AMQP connection and one sender thread per worker process.

    Example:
        >>> LogFactory("INFO").get_logger("chat_service")  # doctest: +SKIP
    """

    def __init__(
        self, log_level: str = "INFO", emitter: Optional[EventHubLogEmitter] = None
    ) -> None:
        """Record the shared level and emitter; open nothing.

        Args:
            log_level: Level name applied to every logger handed out; None/"" -> INFO.
            emitter: The shared Event Hub emitter, or None when forwarding is off.

        Returns:
            None.
        """
        self._log_level = (log_level or "INFO").upper()  # Normalised once, not per logger           # level
        self._emitter = emitter  # None means stdout only, which is the shipped default              # emitter

    @property
    def event_hub_enabled(self) -> bool:
        """True when lines are being forwarded to Event Hub (for the startup log line)."""
        return self._emitter is not None  # Presence of an emitter IS the enabled flag               # enabled?

    def get_logger(self, component_name: str) -> StructuredLogger:
        """A structured logger named for one component."""
        return StructuredLogger(component_name, self._log_level, self._emitter)  # Shares the emitter  # new logger

    def close(self) -> None:
        """Close the shared emitter. Safe when there is none.

        Called LAST in the lifespan teardown, because everything above it may log on its
        way down.
        """
        if self._emitter is not None:  # A no-op when forwarding was never enabled                   # any emitter?
            self._emitter.close()  # Flushes the buffer, then releases the credential                # flush+close


# ============================================ Builder =============================================
def build_log_factory(settings) -> LogFactory:
    """The factory this app runs with, from configuration.

    What this function does:
        - Returns a stdout-only LogFactory unless EVENTHUB_ENABLED and BOTH names are set;
          in that case it builds the emitter, logs that forwarding is on, and returns a
          factory holding it. A construction failure falls back to stdout only.

    Why it exists:
        - Event Hub is opt-in: without EVENTHUB_ENABLED and both names, the factory is built
          with emitter=None and every log line simply goes to stdout as it does today. So
          this ships switched off, and turning it on is an App Setting rather than a deploy.

    Security and production notes:
        1. A failure to construct the emitter is logged and swallowed. Telemetry that cannot
           reach Splunk is a problem; an app that will not start because of it is a worse
           one.
        2. All three conditions are required, so setting the flag alone cannot start
           shipping records to a half-configured destination.

    Args:
        settings: The parsed Settings, read for LOG_LEVEL and the three EVENTHUB_* values.

    Returns:
        A LogFactory, with an emitter only when forwarding is fully configured and built.

    Example:
        >>> build_log_factory(Settings({})).event_hub_enabled
        False
    """
    log = logging.getLogger("orchestrator_api")  # The app logger, for the two startup lines         # app logger
    if not (  # ALL THREE are required: a flag alone must not start shipping records                 # fully set?
        settings.EVENTHUB_ENABLED  # The explicit opt-in switch                                      # flag
        and settings.EVENTHUB_NAMESPACE  # Fully qualified namespace                                 # namespace
        and settings.EVENTHUB_NAME  # The hub to publish to                                          # hub name
    ):
        return LogFactory(log_level=settings.LOG_LEVEL, emitter=None)  # Stdout only: the default    # stdout only
    try:  # Building the emitter opens a connection and can fail                                     # build try
        emitter = EventHubLogEmitter(  # One buffered producer for this worker process               # new emitter
            settings.EVENTHUB_NAMESPACE, settings.EVENTHUB_NAME
        )
        log.info(  # Make it unmistakable in the log that records now leave the tenant               # log info
            "Event Hub log forwarding enabled: %s / %s",
            settings.EVENTHUB_NAMESPACE, settings.EVENTHUB_NAME,
        )
        return LogFactory(log_level=settings.LOG_LEVEL, emitter=emitter)  # Forwarding on            # with emitter
    except Exception:  # Namespace unreachable, role missing, SDK absent -- degrade, don't die       # build failed
        log.exception(  # Telemetry loss is a problem; a dead app is a worse one                     # log error
            "could not start Event Hub log forwarding; continuing with stdout only"
        )
        return LogFactory(log_level=settings.LOG_LEVEL, emitter=None)  # Fall back to stdout         # stdout only
