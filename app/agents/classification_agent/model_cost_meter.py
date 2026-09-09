####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# Compute and log the per-request model cost from token usage using configured per-model prices.   #
#   1. Look up the per-token price entry for the model that produced a response.                   #
#   2. Cost = (prompt_tokens x input_price) + (completion_tokens x output_price).                  #
#   3. Emit a structured cost event (or a missing-price warning) carrying the correlation id.      #
#                                                                                                  #
# Source:-                                                                                         #
#   - From runtime_config ModelPrice is imported.                                                  #
#       - ModelPrice:- the per-model price data structure (input_price / output_price per token)   #
#         used for the cost lookups keyed by model name.                                           #
#   - From telemetry_logging StructuredLogger is imported.                                         #
#       - StructuredLogger:- the structured logger type used to emit the cost / missing-price      #
#         events with a correlation id for end-to-end tracing.                                     #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of type annotations (PEP 563)     # future import

from .runtime_config import ModelPrice  # Per-model price data structure used for cost lookups      # price model
from .telemetry_logging import StructuredLogger  # Structured logger type for emitting cost events  # logger type


# ========================================== Cost tracker =========================================
class CostTracker:  # Computes and logs model usage costs                                           # cost tracker
    """Turn token usage into a cost figure and log it per request.

    What this class is:
        - A tiny cost meter that converts a single model response's token usage into a USD cost,
          using per-token prices keyed by model name, and records the result as a structured event.

    Why this exists:
        - Every model call must be costed. Centralising the arithmetic and the log shape here keeps
          the Foundry gateway free of pricing logic and guarantees a consistent, correlatable cost
          event per request.

    Security and production notes:
        - No secrets or credentials touch this class; it only reads configured prices and token
          counts. A model with no configured price fails safe (cost 0.0 plus a WARNING event)
          rather than raising, so a missing price never breaks a live turn.

    Cost = (prompt_tokens x input_price) + (completion_tokens x output_price),
    using per-token prices keyed by model name from configuration.
    """

    def __init__(self, prices: dict[str, ModelPrice], logger: StructuredLogger) -> None:  # Take the price map and logger
        """Create the cost tracker.

        What this method is:
            - The constructor that captures the immutable dependencies (price map + logger) the
              tracker needs to compute and record costs.

        Why this exists:
            - To inject the configured prices and the structured logger once, so each
              record_usage call stays a pure lookup-and-log with no per-call wiring.

        Args:
            prices: Per-token prices keyed by model name.
            logger: Structured logger used to emit cost events.

        Returns:
            None.

        Example:
            >>> CostTracker({}, logger)  # doctest: +SKIP
        """
        self._prices = prices  # Store the per-model price map for later cost lookups                # price map
        self._logger = logger  # Store the structured logger used to emit cost events                # logger ref

    def record_usage(  # Compute and log the cost of a single model response                        # record cost
        self,  # Instance reference                                                                 # self
        model_name: str,  # Name of the model whose pricing should be looked up                     # model name
        prompt_tokens: int,  # Number of input/prompt tokens consumed                               # input tokens
        completion_tokens: int,  # Number of output/completion tokens produced                      # output tokens
        correlation_id: str,  # End-to-end correlation id used for logging                          # correlation id
    ) -> float:  # Returns the computed cost as a float (USD)                                       # cost usd
        """Compute and log the cost for one model response.

        What this method is:
            - The single public operation: given a model name and its token counts, it returns the
              USD cost and emits exactly one structured log event describing the outcome.

        Why this exists:
            - To give callers a one-line, fail-safe way to record spend per model call while keeping
              the pricing arithmetic and log schema in one place.

        Security and production notes:
            - An unknown model is treated as a non-fatal condition: the method logs a WARNING and
              returns 0.0 rather than raising, so a pricing gap can never surface as an error to the
              calling application. All monetary figures are derived purely from configured prices.

        Args:
            model_name: The model name to look up pricing for.
            prompt_tokens: Prompt (input) token count.
            completion_tokens: Completion (output) token count.
            correlation_id: The end-to-end correlation id.

        Returns:
            The computed cost in USD (0.0 when no price is configured).

        Example:
            >>> tracker.record_usage("gpt-4.1-mini", 100, 20, "cid")  # doctest: +SKIP
            0.000072
        """
        model_price = self._prices.get(model_name)  # Look up the price entry for this model (None if absent)  # price lookup
        if model_price is None:  # If no pricing is configured for this model                       # missing price
            self._logger.log(  # Emit a warning log event noting the missing price                  # log warning
                event="cost_price_missing",  # Event name identifying a missing-price situation     # event name
                correlation_id=correlation_id,  # Correlation id for tracing this log entry          # correlation id
                level="WARNING",  # Log at WARNING severity                                          # log level
                model_name=model_name,  # Include the model name that lacked a price                 # model name
            )
            return 0.0  # Return zero cost since no price is available                               # zero cost

        cost_usd = (  # Compute the total cost in USD                                                # total cost
            prompt_tokens * model_price.input_price  # Input cost: prompt tokens times per-token input price  # input cost
            + completion_tokens * model_price.output_price  # Output cost: completion tokens times per-token output price  # output cost
        )
        self._logger.log(  # Emit a structured log event recording the computed cost                # log cost
            event="model_cost",  # Event name identifying a model cost record                       # event name
            correlation_id=correlation_id,  # Correlation id for tracing this log entry              # correlation id
            model_name=model_name,  # The model the cost applies to                                  # model name
            prompt_tokens=prompt_tokens,  # Number of prompt tokens used                             # input tokens
            completion_tokens=completion_tokens,  # Number of completion tokens used                 # output tokens
            cost_usd=cost_usd,  # The computed cost in USD                                           # cost usd
        )
        return cost_usd  # Return the computed cost to the caller                                    # return cost
