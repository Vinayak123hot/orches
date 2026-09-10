####################################################################################################
# Project name      : IT Support Orchestrator API -- Azure App Service (FastAPI)                   #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# Public face of `app.event_hub`: structured JSON logging, optionally forwarded to Event Hub.      #
#   1. Re-export the four names callers need, so nobody imports the private module path.           #
#   2. Show the three-line usage: build the factory once, get a named logger, log an event.        #
#   3. Record that forwarding is OFF unless EVENTHUB_ENABLED plus both names are configured.       #
#                                                                                                  #
# Source:-                                                                                         #
#   - app.event_hub.telemetry_logging holds the implementation; only these four names are          #
#       re-exported, so a later split of that module does not touch a single caller.               #
####################################################################################################
"""Structured JSON logging, forwarded to Azure Event Hub for Splunk.

What this package is:
    - A thin re-export layer over telemetry_logging.py. The whole usage is three lines:

        from app.event_hub import build_log_factory
        log_factory = build_log_factory(settings)     # once, in deps.py
        logger = log_factory.get_logger("chat_service")
        logger.log(event="turn_completed", correlation_id=conv_id, duration_ms=3204)

Why this exists:
    - So callers import from the package rather than the module, and a later split of
      telemetry_logging.py does not touch a single call site.

Security and production notes:
    1. Off unless EVENTHUB_ENABLED is set WITH a namespace and hub name; until then every
       line just goes to stdout, exactly as it does today. That means it ships disabled and
       enabling it is an App Setting rather than a deploy.
    2. Once enabled, these records LEAVE THE TENANT. Never put user emails, free text,
       raw device output, raw agent responses, exception messages or a URL containing
       ?code= into a field -- see the module docstring in telemetry_logging.py.

Example:
    >>> from app.event_hub import build_log_factory  # doctest: +SKIP
"""

# ============================================ Imports =============================================
from app.event_hub.telemetry_logging import (  # The implementation; only these four are public      # impl
    EventHubLogEmitter,  # Buffered Event Hub sender, used only when forwarding is enabled           # emitter
    LogFactory,  # Hands out StructuredLoggers sharing one level and one emitter                     # factory
    StructuredLogger,  # Emits one JSON record per event on a stable schema                          # logger
    build_log_factory,  # Builds the factory this app runs with, from Settings                       # builder
)

# =========================================== Public API ===========================================
# Explicit, so `from app.event_hub import *` and the linters agree on the surface area.
__all__ = [
    "EventHubLogEmitter",  # Exported for type hints and for tests that stub the emitter             # export
    "LogFactory",  # Exported so deps.py can type the process-wide factory                           # export
    "StructuredLogger",  # Exported so components can type their injected logger                     # export
    "build_log_factory",  # The one function callers actually call                                   # export
]
