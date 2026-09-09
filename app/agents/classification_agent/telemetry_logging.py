####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# Structured JSON logging for this agent, with an optional Event Hub forwarding sink.              #
#   1. Emit structured JSON events with a stable schema (timestamp/event/correlation_id/level).    #
#   2. Filter events below the configured minimum severity level before writing.                   #
#   3. Optionally forward each log line to Azure Event Hub (Managed Identity) for Splunk.          #
#                                                                                                  #
# Why the loggers own their handler:                                                               #
#   The hosting application configures the root logger with its own human-readable format. A       #
#   record that propagated to that root handler would be printed with that prefix, so the line     #
#   would not parse as JSON. Each component logger therefore carries its own stdout handler        #
#   with a raw '%(message)s' formatter and does NOT propagate to the root logger.                  #
#                                                                                                  #
# Source:-                                                                                         #
#   - Standard library json/logging/sys/datetime/typing provide serialisation, the sink,           #
#       the stdout stream, UTC timestamps and the type hints used throughout.                      #
#   - azure.eventhub (EventHubProducerClient / EventData) is imported LAZILY inside the emitter    #
#       so environments that do not enable Event Hub never need the package installed.             #
#   - azure.identity.DefaultAzureCredential authenticates the emitter with Entra ID.               #
####################################################################################################

# ================================================ Imports =========================================
from __future__ import annotations  # Enable postponed evaluation of annotations (PEP 563) for forward refs  # future import

import json  # Serialise log records to JSON strings                                                # stdlib json
import logging  # Standard-library logging used as the underlying sink                              # stdlib logging
import sys  # Access sys.stdout as the log output stream                                            # stdlib sys
from datetime import datetime, timezone  # Build UTC timestamps for each log record                 # stdlib datetime
from typing import Any, Optional  # Type hints for arbitrary field values and optional arguments    # stdlib typing

# Numeric severity order used to filter events below the configured level.
_LEVEL_ORDER: dict[str, int] = {  # Map level name to numeric severity for comparison/filtering     # level map
    "DEBUG": 10,  # Debug severity value                                                            # debug
    "INFO": 20,  # Info severity value                                                              # info
    "WARNING": 30,  # Warning severity value                                                        # warning
    "ERROR": 40,  # Error severity value                                                            # error
    "CRITICAL": 50,  # Critical severity value                                                      # critical
}

# Marks a handler this module installed, so a rebuilt logger is never given a second one.
_HANDLER_MARKER_ATTRIBUTE = "_classification_agent_json_handler"  # Attribute stamped on our handler  # marker attr


# ======================================= Event Hub emitter (optional) =============================
class EventHubLogEmitter:  # Optional sink that forwards log lines to Azure Event Hub
    """Forward each structured log line to Azure Event Hub (for Splunk).

    The azure-eventhub SDK is imported lazily so environments that do not enable
    Event Hub never need the package installed.

    What this class is:
        - A thin, optional forwarding sink that wraps an Azure Event Hub producer client and
          sends one serialised log line per Event Hub event.

    Why this exists:
        - To let structured logs flow to Splunk (via Event Hub) in environments that enable it,
          while keeping the package and the auth entirely out of environments that do not.

    Security and production notes:
        1. Authentication uses Entra ID via DefaultAzureCredential.
        2. Log records must never contain secrets, keys or PII; only structured operational fields.
        3. Emit failures are contained by the caller (StructuredLogger.log) so a logging sink never
           breaks business flow.
    """

    def __init__(self, fully_qualified_namespace: str, event_hub_name: str) -> None:  # Construct emitter for a namespace/hub
        """Create the Event Hub producer using Entra ID auth.

        Args:
            fully_qualified_namespace: The Event Hubs namespace host.
            event_hub_name: The target Event Hub name.

        Returns:
            None.

        Example:
            >>> EventHubLogEmitter("ns.servicebus.windows.net", "logs")  # doctest: +SKIP
        """
        from azure.eventhub import EventHubProducerClient  # Lazy import of the producer client     # lazy import
        from azure.identity import DefaultAzureCredential  # Lazy import of the Entra ID credential  # lazy import

        self._credential = DefaultAzureCredential()  # Create a default Azure credential for auth    # MI credential
        self._producer = EventHubProducerClient(  # Build the Event Hub producer client              # build producer
            fully_qualified_namespace=fully_qualified_namespace,  # Target Event Hubs namespace host  # namespace
            eventhub_name=event_hub_name,  # Target Event Hub name                                   # hub name
            credential=self._credential,  # Credential used to authenticate the producer            # credential
        )

    def emit(self, record_json: str) -> None:  # Send a single serialised log line to Event Hub
        """Send one JSON log line as a single Event Hub event.

        Args:
            record_json: The serialised log record.

        Returns:
            None.

        Example:
            >>> emitter.emit('{"event": "x"}')  # doctest: +SKIP
        """
        from azure.eventhub import EventData  # Lazy import of the event-data wrapper                # lazy import

        event_batch = self._producer.create_batch()  # Create an empty batch to hold events          # new batch
        event_batch.add(EventData(record_json))  # Add the JSON record as one event to the batch     # add event
        self._producer.send_batch(event_batch)  # Send the batch to Event Hub                        # send batch

    def close(self) -> None:  # Release the producer and credential resources
        """Close the producer and the underlying credential.

        Args:
            None.

        Returns:
            None.

        Example:
            >>> emitter.close()  # doctest: +SKIP
        """
        try:  # Attempt to close the producer first                                                 # close producer
            self._producer.close()  # Close the Event Hub producer client                           # producer close
        finally:  # Always run cleanup regardless of producer close outcome                         # cleanup
            self._credential.close()  # Close the Azure credential                                  # credential close


# ============================================ Structured logger ===================================
class StructuredLogger:  # Emits structured JSON log events with a stable schema
    """Emit structured JSON events with a stable schema.

    Every event carries a timestamp, the event name, the correlation id, the
    component name and a level, plus any extra structured fields. Output goes to
    stdout and, when configured, to Event Hub.

    What this class is:
        - The single logging surface used everywhere in this agent: it turns an event name plus
          a correlation id and arbitrary structured fields into one JSON line on a stable schema.

    Why this exists:
        - To make every log line machine-parseable and correlatable end-to-end, and to keep a
          consistent schema across components regardless of which environment is running.

    Security and production notes:
        1. Never place secrets, keys or PII in the event name or the structured fields.
        2. The correlation id ties a turn's events together for diagnostics - it is not a secret.
        3. Event Hub forwarding is best-effort: an emitter failure is swallowed so the business
           flow is never broken by a logging sink.
    """

    def __init__(  # Construct a structured logger for one component
        self,
        component_name: str,  # Name of the component recorded in each event
        min_level: str,  # Minimum severity level this logger will emit
        emitter: Optional[EventHubLogEmitter] = None,  # Optional Event Hub emitter for forwarding
    ) -> None:
        """Create a structured logger for one component.

        What this method is:
            - The constructor: it stores the component identity and severity floor, then attaches
              the raw-JSON stdout handler this logger writes through.

        Why this exists:
            - So each component gets an independently named logger whose output stays valid JSON
              regardless of how the hosting application formats its own root logger.

        Args:
            component_name: The component name recorded in every event.
            min_level: The minimum level to emit (e.g. 'INFO').
            emitter: Optional Event Hub emitter for log forwarding.

        Returns:
            None.

        Example:
            >>> StructuredLogger("classification_turn_service", "INFO")  # doctest: +SKIP
        """
        self._component_name = component_name  # Store the component name for every emitted event   # component
        self._min_level = min_level  # Store the minimum level threshold for filtering              # min level
        self._emitter = emitter  # Store the optional Event Hub emitter                             # emitter
        self._python_logger = logging.getLogger(  # Namespaced stdlib logger backing this component  # stdlib logger
            f"classification_agent.{component_name}"  # Namespace keeps agent logs distinguishable   # logger name
        )
        self._attach_json_handler()  # Give the logger its own raw-JSON stdout handler              # attach handler

    def _attach_json_handler(self) -> None:  # Install a raw-JSON stdout handler exactly once
        """Attach a stdout handler that writes the record verbatim, and stop propagation.

        What this method is:
            - The one-time wiring that makes this logger emit a bare JSON line: a stdout handler
              formatted as '%(message)s', with propagation to the root logger switched off.

        Why this exists:
            - The hosting application configures the root logger with a human-readable prefix. If
              records propagated there, every structured line would be printed with that prefix and
              would not parse as JSON downstream. Owning the handler keeps the contract.

        Security and production notes:
            1. The handler is stamped with a marker attribute and only added when absent, so
               rebuilding a logger for the same component never duplicates log lines.

        Args:
            None.

        Returns:
            None.

        Example:
            >>> logger._attach_json_handler()  # doctest: +SKIP
        """
        for existing_handler in self._python_logger.handlers:  # Inspect handlers already installed  # each handler
            if getattr(existing_handler, _HANDLER_MARKER_ATTRIBUTE, False):  # One of ours is present  # ours?
                return  # Nothing to do - never install a second handler                            # already wired
        json_handler = logging.StreamHandler(stream=sys.stdout)  # Write structured lines to stdout  # build handler
        json_handler.setFormatter(logging.Formatter("%(message)s"))  # Emit the record verbatim (already JSON)  # raw format
        setattr(json_handler, _HANDLER_MARKER_ATTRIBUTE, True)  # Mark it so it is recognised later  # stamp marker
        self._python_logger.addHandler(json_handler)  # Install the handler on this component logger  # add handler
        self._python_logger.setLevel(_LEVEL_ORDER.get(self._min_level, 20))  # Apply the configured floor  # set level
        self._python_logger.propagate = False  # Keep records away from the host's formatted root handler  # no propagate

    def log(self, event: str, correlation_id: str, level: str = "INFO", **fields: Any) -> None:  # Emit one structured event
        """Emit one structured event.

        Args:
            event: The event name (e.g. 'foundry_turn_start').
            correlation_id: The end-to-end correlation id.
            level: The severity level (DEBUG/INFO/WARNING/ERROR/CRITICAL).
            **fields: Additional structured fields to include.

        Returns:
            None.

        Example:
            >>> logger.log(event="foundry_turn_start", correlation_id="cid")  # doctest: +SKIP
        """
        if _LEVEL_ORDER.get(level, 20) < _LEVEL_ORDER.get(self._min_level, 20):  # Skip events below the min level  # level filter
            return  # Drop the event when its severity is below the threshold                       # drop event

        record: dict[str, Any] = {  # Build the base structured log record                          # base record
            "timestamp": datetime.now(timezone.utc).isoformat(),  # Current UTC time in ISO-8601 format  # timestamp
            "event": event,  # The event name                                                       # event name
            "correlation_id": correlation_id,  # The end-to-end correlation id                      # correlation id
            "agent_name": self._component_name,  # The component name for this logger               # component
            "level": level,  # The severity level of this event                                     # level
        }
        record.update(fields)  # Merge any extra structured fields into the record                  # merge fields
        record_json = json.dumps(record, ensure_ascii=False, default=str)  # Serialise record to JSON, stringifying unknowns  # to json

        self._python_logger.log(_LEVEL_ORDER.get(level, 20), record_json)  # Write JSON at the mapped level  # write stdout
        if self._emitter is not None:  # Only forward when an emitter is configured                 # emitter set?
            # A logging sink must never break business flow.
            try:  # Attempt to forward the record to Event Hub                                      # try emit
                self._emitter.emit(record_json)  # Send the JSON record via the emitter             # emit
            except Exception:  # Swallow any emitter failure to protect business flow               # emit failed
                self._python_logger.exception("Failed to emit log to Event Hub.")  # Log the emit failure  # log failure


# =============================================== Log factory ======================================
class LogFactory:  # Creates StructuredLogger instances sharing level and emitter config
    """Create StructuredLogger instances that share level + emitter config.

    What this class is:
        - A small factory that hands out StructuredLogger instances which all share the same
          minimum level and the same optional Event Hub emitter.

    Why this exists:
        - To centralise logging configuration so every component logs consistently to stdout
          (and optionally Event Hub) without each call site repeating the setup.

    Security and production notes:
        1. The shared emitter authenticates with Entra ID via DefaultAzureCredential.
        2. Records carry only structured operational fields; add no secrets or PII.
        3. close() is safe to call when no emitter is configured (no-op).
    """

    def __init__(self, log_level: str = "INFO", emitter: Optional[EventHubLogEmitter] = None) -> None:  # Hold shared settings
        """Hold the shared logger settings used for every logger this factory builds.

        What this method is:
            - The constructor: it normalises the configured level and keeps the optional shared
              emitter that every logger produced here will forward through.

        Why this exists:
            - So the level and sink are decided once, at composition time, rather than at each
              call site that needs a logger.

        Security and production notes:
            1. The hosting application owns the root logger; each StructuredLogger carries its own
               raw-JSON handler and writes through that.

        Args:
            log_level: The minimum level emitted by all loggers.
            emitter: Optional shared Event Hub emitter.

        Returns:
            None.

        Example:
            >>> LogFactory(log_level="INFO")  # doctest: +SKIP
        """
        self._log_level = log_level.upper()  # Normalise the configured level to uppercase           # normalise level
        self._emitter = emitter  # Store the shared optional Event Hub emitter                       # shared emitter

    def get_logger(self, component_name: str) -> StructuredLogger:  # Build a StructuredLogger for a component
        """Return a structured logger for a named component.

        Args:
            component_name: The component name.

        Returns:
            A configured StructuredLogger.

        Example:
            >>> factory.get_logger("foundry_agent_gateway")  # doctest: +SKIP
        """
        return StructuredLogger(component_name, self._log_level, self._emitter)  # Share level and emitter  # build logger

    def close(self) -> None:  # Release the shared emitter if present
        """Close any shared emitter (safe to call when none exists).

        Args:
            None.

        Returns:
            None.

        Example:
            >>> factory.close()  # doctest: +SKIP
        """
        if self._emitter is not None:  # Only close when an emitter was configured                  # emitter set?
            self._emitter.close()  # Close the shared Event Hub emitter                             # emitter close
