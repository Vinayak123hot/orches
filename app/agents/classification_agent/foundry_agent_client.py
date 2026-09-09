####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# Instrumented gateway to the Foundry agent via the Responses API (agent_reference + conversation).#
#   1. Borrow the application's already-authenticated Foundry project client (no second handshake).#
#   2. Send each turn against a stable conversation id so Foundry carries the history server-side. #
#   3. Bound every call with a per-call timeout + exponential-backoff retry on transient errors.   #
#   4. Record response token usage as a logged cost figure.                                        #
#                                                                                                  #
# Where the connection comes from:                                                                 #
#   The hosting application builds ONE authenticated Foundry client per worker process and injects #
#   it here. This gateway never constructs a credential or a project client of its own, so there   #
#   is exactly one Entra handshake per worker no matter how many agents run in it.                 #
#                                                                                                  #
# Source:-                                                                                         #
#   - From model_cost_meter CostTracker is imported.                                               #
#       - CostTracker:- turns response token usage into a logged cost figure.                      #
#   - From telemetry_logging LogFactory / StructuredLogger are imported.                           #
#       - LogFactory / StructuredLogger:- logger factory + structured logger type.                 #
#   - From backoff_retry run_with_retry is imported.                                               #
#       - run_with_retry:- generic exponential-backoff retry runner for transient errors.          #
#   - azure.core.exceptions + openai supply the transient SDK error types worth retrying.          #
####################################################################################################

# ===================================== Imports =====================================
from __future__ import annotations  # Enable postponed evaluation of type annotations (PEP 563)     # future import

from typing import Any  # Generic type hints for SDK objects and request payloads                   # type hints

from azure.core.exceptions import AzureError, ServiceRequestError, ServiceResponseError  # Azure SDK error types  # azure errors
from openai import (  # OpenAI SDK errors (the Responses client handed out by the project client is an openai client)  # openai errors
    APIConnectionError,  # Network/connection failure                                               # conn error
    APITimeoutError,  # Request timeout                                                             # timeout error
    InternalServerError,  # HTTP 5xx                                                                # server error
    OpenAIError,  # Base OpenAI SDK error                                                           # base error
    RateLimitError,  # HTTP 429                                                                     # rate limit
)

from .backoff_retry import run_with_retry  # Generic exponential-backoff retry runner                # backoff retry
from .model_cost_meter import CostTracker  # Turns response token usage into a logged cost figure    # cost meter
from .telemetry_logging import LogFactory, StructuredLogger  # Logger factory + structured logger type  # telemetry

# Transient errors (Azure transport + OpenAI) worth retrying with backoff.
_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (  # Exceptions eligible for a retry           # retryable set
    ServiceRequestError,  # Azure request-send/network failure                                      # azure send
    ServiceResponseError,  # Azure incomplete/failed response (incl. read timeouts)                 # azure response
    APIConnectionError,  # OpenAI connection failure                                                # openai conn
    APITimeoutError,  # OpenAI request timeout                                                      # openai timeout
    InternalServerError,  # OpenAI 5xx                                                              # openai 5xx
    RateLimitError,  # OpenAI 429                                                                   # openai 429
)

# Configured values that leave version selection to Foundry.
_UNPINNED_VERSION_VALUES: frozenset[str] = frozenset({"", "default", "latest", "none"})  # Omit the version key  # unpinned set


# ===================================== Exceptions =====================================
class FoundryAgentError(Exception):  # Domain error raised when a Foundry Agent Service call fails/times out
    """Raised when a Foundry Agent Service operation fails or times out.

    What this class is:
        - The single domain-level exception surfaced by this gateway. Every SDK / transport failure
          is mapped to this type so callers never see a raw Azure/OpenAI stack trace.

    Why this exists:
        - To keep the boundary clean: the turn service catches ONE error type and maps it to a safe
          fallback instead of reasoning about every underlying transient/permanent SDK error.
    """


# =================== Foundry Agent Gateway (Responses API + agent_reference) ====================
class FoundryAgentGateway:  # Instrumented wrapper over the injected Foundry Responses client
    """Thin, instrumented gateway for the Foundry agent via the Responses API.

    What this class is:
        - A thin, instrumented wrapper over the Foundry project client the hosting application
          injects. It owns exactly one remote operation: sending one turn to the agent.

    Why this exists:
        - To centralise the exact Foundry call surface (agent_reference + conversation), the bounded
          timeout/retry policy, and cost tracking behind one small, testable object.

    How a turn is addressed:
        The agent is selected per call through
        extra_body={"agent_reference": {"name", "version", "type": "agent_reference"}}.
        Conversation state is held server-side by a Foundry conversation whose id is STABLE for the
        whole conversation and is supplied by the calling application, so Foundry keeps the running
        history and each reply is only that turn's output.

    Security and production notes:
        1. Authentication belongs to the injected client; this gateway neither holds nor logs a
           credential of its own.
        2. Every remote call is bounded by a per-call timeout and exponential-backoff retry, and
           response token usage is recorded as cost for observability.
        3. The gateway owns no connection, credential or thread pool, so it needs no teardown and
           cannot leak resources when the hosting application recycles a worker.
    """

    def __init__(  # Configure the injected client, agent selection, timeout, retry policy and cost tracking
        self,
        foundry_client: Any,  # The application's Foundry client, exposing an `openai` Responses client  # foundry client
        agent_name: str,  # Name of the pre-created Foundry agent                                    # agent name
        agent_version: str,  # Concrete version to pin; blank/'default' lets Foundry choose          # version
        request_timeout_seconds: int,  # Per-call timeout in seconds                                 # timeout
        cost_tracker: CostTracker,  # Injected cost tracker for response usage accounting            # cost tracker
        agent_model_name: str,  # Model name used for cost lookup/logging                            # model name
        log_factory: LogFactory,  # Factory used to obtain a structured logger                       # log factory
        retry_max_attempts: int,  # Maximum attempts per remote call                                 # retry attempts
        retry_base_delay_seconds: float,  # Base delay for exponential backoff                       # base delay
        retry_max_delay_seconds: float,  # Upper bound on a single backoff wait                      # max delay
    ) -> None:
        """Store the injected client and the call policy. Performs no network work.

        What this method is:
            - The constructor: it only keeps references and configuration. No credential, client or
              network call is created here - the injected client builds itself on first use.

        Why this exists:
            - To keep construction cheap and side-effect free so the worker can build the gateway
              once and only touch Foundry when a turn actually arrives.

        Security and production notes:
            1. This gateway accepts no credential; authentication belongs to the injected client,
               which resolves it when first used.
            2. The timeout / retry / cost-tracking knobs are injected from config so operational
               limits are auditable and not hard-coded.

        Args:
            foundry_client: The application's Foundry client, exposing an `openai` Responses client.
            agent_name: Name of the pre-created Foundry agent.
            agent_version: A concrete version to pin, or blank/'default' to let Foundry choose.
            request_timeout_seconds: Per-call timeout in seconds.
            cost_tracker: Tracker used to log per-response cost.
            agent_model_name: Model name used for cost lookup/logging.
            log_factory: Factory used to obtain a structured logger.
            retry_max_attempts: Max attempts for a single remote call.
            retry_base_delay_seconds: Base backoff delay.
            retry_max_delay_seconds: Upper bound on a single backoff wait.

        Returns:
            None.

        Example:
            >>> FoundryAgentGateway(  # doctest: +SKIP
            ...     foundry_client, "clasification-agent", "", 60, cost_tracker,
            ...     "gpt-4.1-mini", log_factory, 3, 0.5, 8.0)
        """
        self._foundry_client = foundry_client  # Keep the injected, already-authenticated client     # foundry client
        self._agent_name = agent_name  # Store the agent name                                        # agent name
        self._configured_agent_version = agent_version  # Store the configured version               # version
        self._request_timeout_seconds = request_timeout_seconds  # Store the per-call timeout        # timeout
        self._cost_tracker = cost_tracker  # Store the cost tracker                                  # cost tracker
        self._agent_model_name = agent_model_name  # Store the model name for cost lookup            # model name
        self._logger: StructuredLogger = log_factory.get_logger("foundry_agent_gateway")  # Named structured logger  # logger
        self._retry_max_attempts = retry_max_attempts  # Store max retry attempts                    # retry attempts
        self._retry_base_delay_seconds = retry_base_delay_seconds  # Store backoff base delay        # base delay
        self._retry_max_delay_seconds = retry_max_delay_seconds  # Store backoff max delay           # max delay

    # ===================================== Client access =====================================
    def _responses_client(self) -> Any:  # Return the Responses client bound to this agent's call policy
        """Return the injected Responses client, bounded by this agent's own timeout.

        What this method is:
            - The single access point to the injected client. It applies this agent's per-call
              timeout and disables the SDK's own retry layer, because retries are owned here.

        Why this exists:
            - The application sizes its shared client for its own short calls. A classification turn
              is longer, and the application's configuration explicitly expects an agent that needs
              more time to supply its own per-call bound rather than widening the shared default.

        Security and production notes:
            1. Turning off the SDK's internal retries keeps ONE retry layer in play, so the worst
               case is attempts x timeout rather than the product of two independent policies. Where
               the client cannot be re-optioned, the timeout is passed per request instead and the
               SDK's own retry count still applies on top.

        Args:
            None.

        Returns:
            A Responses-capable client honouring this agent's timeout.

        Example:
            >>> gateway._responses_client()  # doctest: +SKIP
            <OpenAI ...>
        """
        openai_client = self._foundry_client.openai  # Borrow the application's Responses client     # borrow client
        if hasattr(openai_client, "with_options"):  # Preferred path: derive a client with our policy  # can re-option?
            return openai_client.with_options(  # Copy the client with this agent's call policy      # re-option
                timeout=self._request_timeout_seconds,  # Bound one call to this agent's timeout     # timeout
                max_retries=0,  # Retries are owned by run_with_retry, not by the SDK                # no sdk retry
            )
        return openai_client  # Fallback: use the shared client and pass the timeout per request     # fallback client

    def _guarded_call(  # Run one remote operation with retries and a uniform error policy
        self, operation: Any, operation_name: str, correlation_id: str  # Operation, its name, correlation id
    ) -> Any:
        """Execute a remote operation with retry/backoff and map every failure to one error type.

        What this method is:
            - The single choke point for every remote call: it wraps the operation in the retry
              runner and converts any failure into FoundryAgentError.

        Why this exists:
            - To apply one uniform retry + logging + domain-error policy to every call, so the turn
              service reasons about exactly one failure type.

        Security and production notes:
            1. Transient errors are retried with exponential backoff; each attempt is bounded by the
               per-call timeout applied to the client.
            2. Failures are logged with the correlation id and re-raised as FoundryAgentError, so no
               raw SDK exception (or its message internals) leaks to the caller.

        Args:
            operation: A zero-argument callable performing the remote call.
            operation_name: A short name for the operation (used in logs).
            correlation_id: The end-to-end correlation id.

        Returns:
            Whatever the operation returns.

        Raises:
            FoundryAgentError: If the operation fails after retries.

        Example:
            >>> gateway._guarded_call(lambda: 2, "noop", "cid")  # doctest: +SKIP
            2
        """
        try:  # Run the operation through the retry runner and normalise any failure                # guarded run
            return run_with_retry(  # Retry the operation on transient errors                        # retry call
                operation,  # The remote call to execute/retry                                       # operation
                correlation_id=correlation_id,  # Correlation id for retry logs                      # correlation
                operation_name=operation_name,  # Operation label for logs                           # op name
                logger=self._logger,  # Structured logger for retry events                           # logger
                max_attempts=self._retry_max_attempts,  # Max attempts from config                    # attempts
                base_delay_seconds=self._retry_base_delay_seconds,  # Backoff base delay from config  # base delay
                max_delay_seconds=self._retry_max_delay_seconds,  # Backoff max delay from config     # max delay
                retryable_exceptions=_RETRYABLE_EXCEPTIONS,  # Which exceptions trigger a retry       # retryable
            )
        except (AzureError, OpenAIError, ValueError) as call_error:  # Failed after retries, or returned bad data  # failure
            self._logger.log(  # Log the failure                                                     # log failure
                event="foundry_call_failed",  # Event name                                           # event
                correlation_id=correlation_id,  # Correlate with this request                        # correlation
                level="ERROR",  # Severity level                                                     # level
                operation_name=operation_name,  # Which operation failed                             # op name
                error_type=type(call_error).__name__,  # Exception class name                        # error type
                error_message=str(call_error),  # Exception message                                   # error msg
            )
            raise FoundryAgentError(f"Foundry operation '{operation_name}' failed.") from call_error  # Domain error  # raise

    # ===================================== Agent selection =====================================
    def _reference_version(self) -> str | None:  # Version string to put in the agent_reference, or None to omit
        """Return the version to send in the agent_reference, or None to omit the key.

        What this method is:
            - The policy that decides which version (if any) identifies the agent on a call.

        Why this exists:
            - Pinning a concrete version makes a deployment reproducible, while omitting the key
              lets Foundry resolve the agent's own default. Both are legitimate; the configuration
              chooses, and nothing here has to query the service to find out.

        Args:
            None.

        Returns:
            The version string to send, or None to omit the version key.

        Example:
            >>> gateway._reference_version()  # doctest: +SKIP
            '9'
        """
        configured_version = (self._configured_agent_version or "").strip()  # Normalise the configured value  # normalise
        if configured_version.lower() in _UNPINNED_VERSION_VALUES:  # Blank / 'default' / 'latest' / 'none'  # unpinned?
            return None  # Omit the version key and let Foundry resolve the agent's default          # omit
        return configured_version  # Send the pinned version as-is (e.g. '9')                        # pinned

    # ===================================== Public API =====================================
    def create_response(  # Send input to the agent within a conversation and return the reply text
        self, input_text: str, conversation_id: str, correlation_id: str  # Input, conversation id, correlation id
    ) -> str:
        """Send input to the agent via the Responses API and return its reply text.

        The agent is selected per call through extra_body agent_reference. The call is part of the
        supplied conversation, so Foundry carries the history server-side and the returned text is
        ONLY this turn's reply.

        What this method is:
            - The core turn method: it builds the agent_reference, sends the input on the Responses
              API, records cost, and returns just this turn's reply text.

        Why this exists:
            - To expose one simple call surface for the turn service, hiding agent selection,
              conversation attachment, timeout/retry, and cost accounting.

        Security and production notes:
            1. The agent is selected per call via extra_body agent_reference (name + optional
               version); authentication belongs to the injected client.
            2. The call is bounded by timeout/retry and its token usage is recorded as cost.
            3. Only this turn's output_text is returned; Foundry keeps the running history
               server-side under the conversation id.

        Args:
            input_text: The text to send (user message, or fed-back candidates).
            conversation_id: The Foundry conversation id carrying this conversation's history.
            correlation_id: The end-to-end correlation id.

        Returns:
            The agent's reply text for this turn (expected to be a strict JSON object).

        Raises:
            FoundryAgentError: If the response call fails after retries.

        Example:
            >>> gateway.create_response("outlook crashes", "conv_abc", "cid")  # doctest: +SKIP
            '{"status": "search", ...}'
        """
        responses_client = self._responses_client()  # Client bound to this agent's timeout policy   # get client
        agent_reference: dict[str, Any] = {  # Select the agent for this call                        # agent ref
            "name": self._agent_name,  # The agent name                                              # agent name
            "type": "agent_reference",  # Reference type required by Foundry                         # ref type
        }
        reference_version = self._reference_version()  # Version to pin, or None to omit             # resolve version
        if reference_version:  # Only include the version when one is configured                     # version?
            agent_reference["version"] = reference_version  # Pin the agent version in the reference  # pin version
        request_kwargs: dict[str, Any] = {  # Assemble the Responses API call arguments              # request kwargs
            "input": [{"role": "user", "content": input_text}],  # The input for this call           # input
            "conversation": conversation_id,  # Attach to the conversation carrying the history      # attach conv
            "extra_body": {"agent_reference": agent_reference},  # Select the agent per call         # extra body
        }
        if not hasattr(responses_client, "with_options"):  # Fallback client could not carry our policy  # fallback?
            request_kwargs["timeout"] = self._request_timeout_seconds  # Bound this request directly  # per-call timeout
        response = self._guarded_call(  # Call the Responses API under retry + one error policy      # guarded call
            lambda: responses_client.responses.create(**request_kwargs),  # Remote call: create a response  # sdk create
            operation_name="responses.create",  # Operation label for logs                           # op name
            correlation_id=correlation_id,  # Correlation id for tracing                             # correlation
        )
        self._record_cost(response, correlation_id)  # Turn the response's token usage into a logged cost  # record cost
        return str(getattr(response, "output_text", "") or "")  # Return ONLY this turn's reply text  # return text

    # ===================================== Internal Helpers =====================================
    def _record_cost(self, response: Any, correlation_id: str) -> None:  # Derive and log cost from response usage
        """Extract token usage from a Responses result and record its cost.

        What this method is:
            - The cost accountant: it reads token usage off the Responses result and forwards it to
              the injected cost tracker.

        Why this exists:
            - To make every model call observable/costed without the turn service having to know the
              usage field names.

        Security and production notes:
            1. Usage is read defensively (getattr with fallbacks) so a missing/renamed field never
               raises - absent usage simply skips cost recording.

        Args:
            response: The Responses API result (expected to expose a `usage` object).
            correlation_id: The end-to-end correlation id.

        Returns:
            None.

        Example:
            >>> gateway._record_cost(response, "cid")  # doctest: +SKIP
        """
        usage = getattr(response, "usage", None)  # Safely read the response usage object            # read usage
        if usage is None:  # Nothing to record when usage is absent                                  # no usage
            return  # Skip cost recording                                                            # skip
        prompt_tokens = getattr(usage, "input_tokens", None)  # Responses API names input tokens 'input_tokens'  # input tokens
        if prompt_tokens is None:  # Fall back to the chat-style name if needed                      # fallback?
            prompt_tokens = getattr(usage, "prompt_tokens", 0)  # Chat-style prompt token count      # prompt tokens
        completion_tokens = getattr(usage, "output_tokens", None)  # Responses API names output tokens 'output_tokens'  # output tokens
        if completion_tokens is None:  # Fall back to the chat-style name if needed                  # fallback?
            completion_tokens = getattr(usage, "completion_tokens", 0)  # Chat-style completion token count  # completion tokens
        self._cost_tracker.record_usage(  # Forward token counts to the cost tracker                 # record usage
            model_name=self._agent_model_name,  # Model used for cost-rate lookup                    # model name
            prompt_tokens=int(prompt_tokens or 0),  # Prompt/input tokens (default 0)                # prompt tokens
            completion_tokens=int(completion_tokens or 0),  # Completion/output tokens (default 0)   # completion tokens
            correlation_id=correlation_id,  # Correlate the usage record with the request            # correlation
        )
