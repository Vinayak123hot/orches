####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
#   1. Provide a bounded exponential-backoff retry wrapper (run_with_retry) for remote calls.      #
#   2. Apply equal jitter so concurrent callers do not retry in lockstep after a shared 429.       #
#   3. Honor a server-provided Retry-After hint, clamped to the configured maximum delay.          #
#   4. Emit structured retry / exhaustion log events carrying the end-to-end correlation id.       #
#                                                                                                  #
# Source:-                                                                                         #
#   - From app.event_hub StructuredLogger is imported.                                             #
#       - StructuredLogger:- typed structured logger used to emit retry and exhaustion events      #
#         keyed by correlation_id for end-to-end tracing.                                          #
#   - random (stdlib) supplies the jitter that de-synchronises concurrent retries.                 #
#   - time (stdlib) supplies sleep() used for the computed backoff delay.                          #
#   - typing supplies Callable / TypeVar for the operation callable and its generic result type.   #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of type annotations (PEP 563)     # future import

import random  # Provides jitter to avoid synchronized retries (thundering herd)                    # stdlib random
import time  # Provides sleep() used for the backoff delay                                          # stdlib time
from typing import Callable, TypeVar  # Type hints for the operation callable and generic result type  # typing hints

from app.event_hub import StructuredLogger  # Typed structured logger for retry events              # logger type

_ResultType = TypeVar("_ResultType")  # Generic type variable representing the operation's return type  # result typevar


# ========================================= Retry wrapper =========================================
def run_with_retry(  # Run a callable with bounded exponential-backoff retries
    operation: Callable[[], _ResultType],  # Zero-argument callable to execute and possibly retry
    *,  # Force all following parameters to be keyword-only
    correlation_id: str,  # End-to-end correlation id included in log events
    operation_name: str,  # Short human-readable name for the operation in logs
    logger: StructuredLogger,  # Logger used to emit retry and exhaustion events
    max_attempts: int,  # Maximum number of attempts before giving up
    base_delay_seconds: float,  # Base delay used for the first backoff wait
    max_delay_seconds: float,  # Upper bound clamp on any single backoff wait
    retryable_exceptions: tuple[type[Exception], ...],  # Exception types that trigger a retry
) -> _ResultType:  # Returns the result of the successful operation call
    """Run a callable with bounded exponential backoff on retryable errors.

    What this function is:
        - A single, provider-agnostic retry harness for remote calls. It wraps ANY zero-argument
          callable and adds bounded exponential backoff, equal jitter, and Retry-After awareness
          around it.

    Why this exists:
        - Remote services fail transiently (429 rate limits, 503s, socket timeouts). A shared,
          centrally logged retry policy keeps that resilience identical across every caller and
          out of the business logic, so a transient blip does not surface as a hard failure.

    Security and production notes:
        - Non-retryable exceptions propagate immediately and are never swallowed here; only the
          caller-declared retryable_exceptions are retried, up to max_attempts, doubling the delay
          each time up to max_delay_seconds. Every retry and the final exhaustion are logged with
          the correlation id (no secrets are logged; only error type / message / timing).
        - Equal jitter de-synchronises concurrent callers so they do not stampede a recovering
          service; a server Retry-After hint is honored but always clamped to max_delay_seconds so
          a hostile / buggy hint can never make the worker sleep unbounded.
        - This wrapper bounds ATTEMPTS, not wall clock. The caller is responsible for any overall
          deadline; the turn loop enforces one so a chat turn cannot outlive its budget.

    Args:
        operation: The zero-argument callable to execute.
        correlation_id: The end-to-end correlation id.
        operation_name: A short name for the operation (used in logs).
        logger: Structured logger used for retry/exhaustion events.
        max_attempts: Maximum number of attempts (>= 1).
        base_delay_seconds: Base backoff delay for the first retry.
        max_delay_seconds: Upper bound on any single backoff wait.
        retryable_exceptions: Exception types that should trigger a retry.

    Returns:
        The result of the successful call.

    Raises:
        Exception: The last retryable exception if all attempts fail, or any
            non-retryable exception immediately.

    Example:
        >>> run_with_retry(lambda: 2, correlation_id="cid", operation_name="noop",
        ...     logger=logger, max_attempts=3, base_delay_seconds=0.5,
        ...     max_delay_seconds=8.0, retryable_exceptions=(TimeoutError,))  # doctest: +SKIP
        2
    """
    attempt_number = 1  # Track the current attempt count, starting at the first attempt            # attempt counter
    while True:  # Loop until the operation succeeds or the retries are exhausted                   # retry loop
        try:  # Attempt to run the operation                                                        # try op
            return operation()  # Call the operation and return its result on success               # run op
        except retryable_exceptions as retryable_error:  # Only handle exceptions deemed retryable  # retryable path
            if attempt_number >= max_attempts:  # If no attempts remain, give up                    # exhausted?
                logger.log(  # Log a final exhaustion event                                         # log exhausted
                    event="retry_exhausted",  # Event name for exhausted retries                    # event name
                    correlation_id=correlation_id,  # Correlation id for tracing                    # correlation id
                    level="ERROR",  # Log at ERROR severity                                         # severity
                    operation_name=operation_name,  # Name of the failed operation                  # op name
                    attempts=attempt_number,  # Total number of attempts made                       # attempts
                    error_type=type(retryable_error).__name__,  # Class name of the last error      # error type
                    error_message=str(retryable_error),  # Message of the last error                # error message
                )
                raise  # Re-raise the last retryable exception to the caller                        # propagate

            # Base exponential backoff, doubling per attempt, clamped to the max.
            base_backoff_seconds = min(
                max_delay_seconds, base_delay_seconds * (2 ** (attempt_number - 1))
            )
            # Equal jitter: keep half the backoff as a floor and randomise the other
            # half so concurrent callers do not retry in lockstep after a shared 429.
            delay_seconds = (base_backoff_seconds / 2.0) + random.uniform(0.0, base_backoff_seconds / 2.0)
            # Honor a server-provided Retry-After hint (e.g. HTTP 429/503) when the
            # failing operation attached one, still clamped to the configured max.
            retry_after_seconds = getattr(retryable_error, "retry_after_seconds", None)  # Optional hint on the exception
            if isinstance(retry_after_seconds, (int, float)) and retry_after_seconds > delay_seconds:  # Hint asks for longer
                delay_seconds = min(max_delay_seconds, float(retry_after_seconds))  # Wait as told, but never beyond the max
            logger.log(  # Log that a retry is about to happen                                      # log retry
                event="retry_attempt",  # Event name for a retry attempt                            # event name
                correlation_id=correlation_id,  # Correlation id for tracing                        # correlation id
                level="WARNING",  # Log at WARNING severity                                         # severity
                operation_name=operation_name,  # Name of the operation being retried               # op name
                attempt=attempt_number,  # The attempt number that just failed                      # attempt
                next_delay_seconds=delay_seconds,  # How long we will wait before retrying          # next delay
                error_type=type(retryable_error).__name__,  # Class name of the current error       # error type
                error_message=str(retryable_error),  # Message of the current error                 # error message
            )
            time.sleep(delay_seconds)  # Wait for the computed backoff delay                        # sleep
            attempt_number += 1  # Increment the attempt counter before looping again               # advance
