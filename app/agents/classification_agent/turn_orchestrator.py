####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# Coordinate ONE classification turn - run the Foundry agent over a stable conversation and, on a  #
# 'search' request, hand it the knowledge base entirely IN-PROCESS (no HTTP loopback).             #
#   1. Validate the {conv_id, message} boundary and continue the caller's Foundry conversation.    #
#   2. On agent status 'search', feed the ENTIRE candidate set back so the AGENT selects.          #
#   3. Guard resolutions against fabricated article ids and shape a safe response for every outcome.#
#   4. Hold every turn inside a wall-clock budget so one chat turn cannot run unbounded.           #
#                                                                                                  #
# Source:-                                                                                         #
#   - runtime_config supplies load_settings and the search endpoint's connection details.          #
#   - correlation_ids supplies generate_correlation_id (log key when a conversation id is blank).  #
#   - model_cost_meter supplies CostTracker (turns response token usage into a logged cost).       #
#   - telemetry_logging supplies EventHubLogEmitter / LogFactory / StructuredLogger.               #
#   - service_contracts supplies AgentEntryRequest / AgentEntryResponse / AgentStructuredOutput.   #
#   - foundry_agent_client supplies FoundryAgentGateway + FoundryAgentError.                       #
#   - servicenow_kb_source supplies KnowledgeBaseSource + KnowledgeBaseSourceError.                #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of type annotations (PEP 563)     # future import

import json  # Parse the agent's strict-JSON reply and serialise candidates                        # stdlib json
import re  # Strip accidental markdown code fences from the agent's reply                          # stdlib re
import threading  # Lock guarding one-time service construction across worker threads              # stdlib threading
import time  # Monotonic clock backing the per-turn wall-clock budget                               # stdlib time
from typing import Any, Optional  # Generic type hints for JSON dicts and optional values          # stdlib typing

from pydantic import ValidationError  # Raised when a boundary payload fails schema validation      # boundary error

from .correlation_ids import generate_correlation_id  # Log key used when no conversation id is available  # log id
from .foundry_agent_client import FoundryAgentError, FoundryAgentGateway  # Instrumented gateway + its error  # foundry client
from .model_cost_meter import CostTracker  # Turns response token usage into a logged cost figure    # cost meter
from .runtime_config import (  # Validated config and the search endpoint's connection details       # config loader
    load_servicenow_credentials,  # Search endpoint connection details, read from the environment    # credentials
    load_settings,  # This agent's validated section of the application config                       # settings
)
from .service_contracts import AgentEntryRequest, AgentEntryResponse, AgentStructuredOutput  # Turn boundary models  # contracts
from .servicenow_kb_source import KnowledgeBaseSource, KnowledgeBaseSourceError  # KB source + its error  # kb source
from .servicenow_search_client import ServiceNowSearchClient  # Client for the knowledge search endpoint  # search client
from .servicenow_token_provider import (  # Supplies the bearer token the search is called with      # token provider
    ClientCredentialsTokenProvider,  # Requests a token and holds it until it nears expiry           # requested
    StaticTokenProvider,  # Presents a token that was supplied directly                              # supplied
)
from .telemetry_logging import EventHubLogEmitter, LogFactory, StructuredLogger  # Logging: emitter, factory, logger  # telemetry

# Strips accidental ```json ... ``` fences from the agent's reply.
_JSON_FENCE_PATTERN = re.compile(r"^```(?:json)?|```$", re.IGNORECASE | re.MULTILINE)  # Opening/closing fence matcher  # fence regex

# Safe, human-facing fallbacks.
_SAFE_FALLBACK_MESSAGE = (  # Generic apology shown to the user on any internal failure              # safe fallback
    "Sorry, something went wrong while processing your request. Please try again in a moment."
)
_NO_MATCH_FALLBACK_MESSAGE = (  # Shown when the turn ends without the agent reaching a decision     # no_match fallback
    "I'm sorry, I couldn't find the right article for this. I've logged it and one of our "
    "team members will reach out to you."
)

# Process-wide singleton so the service, gateway and knowledge-base source are built once per worker.
_CACHED_TURN_SERVICE: "ClassificationTurnService | None" = None  # Lazily-built service, cached for the worker's life  # cache slot
_TURN_SERVICE_LOCK = threading.Lock()  # Guards lazy construction so concurrent cold requests build only once  # build lock


# ========================================== Turn service ==========================================
class ClassificationTurnService:  # Coordinates one turn against the Foundry agent (in-process KB feed)  # turn service
    """Drive one turn of the Outlook Support Classification Agent.

    What this class is:
        The agent cannot call tools directly, so THIS service supplies the knowledge base
        IN-PROCESS (no HTTP): each call to the agent goes through the Responses API
        (agent_reference), and conversation state is held server-side by a Foundry
        conversation whose id is the caller's stable conv_id. When the agent replies with
        status "search" and a query, this service feeds the ENTIRE candidate set back into
        the same conversation and lets the AGENT select; it repeats until the agent returns
        follow_up / resolved / no_match, the search budget is spent, or the turn's
        wall-clock budget expires.

    Why the whole candidate set goes back:
        Selection is the agent's job. Handing it every candidate keeps article choice in exactly
        one place, so the article the agent reasoned about is the article that is returned.

    Security and production notes:
        1. Any failure yields a safe error response - no exception ever propagates to the caller.
        2. A resolved kb_id is accepted ONLY if it is a REAL entry in the index (fabricated ids
           are rejected); the whole index is validated, not just this turn's candidates.
        3. Logs are keyed by conv_id so a turn can be traced end-to-end.
        4. A turn is bounded by BOTH a search-round budget and a wall-clock budget, so a chat turn
           the caller is waiting on cannot run for an unbounded time.
    """

    def __init__(  # Wire the service's collaborators                                               # constructor
        self,
        foundry_agent_gateway: FoundryAgentGateway,  # Instrumented Foundry gateway                 # gateway
        knowledge_source: KnowledgeBaseSource,  # In-process source feeding every candidate to the agent  # kb source
        agent_name: str,  # Configured agent name (used for a config guard)                         # agent name
        max_search_rounds: int,  # Max knowledge-base searches per turn before forcing a handoff    # search budget
        turn_budget_seconds: float,  # Wall-clock budget for one whole turn                         # time budget
        logger: StructuredLogger,  # Structured logger for turn events                              # logger
    ) -> None:
        """Create the turn service.

        What this does:
            Stores the collaborators the turn loop needs (gateway, knowledge-base source, config
            guards, budgets, and the logger).

        Args:
            foundry_agent_gateway: The instrumented Foundry gateway.
            knowledge_source: The in-process source that returns the candidate set for the agent
                to select from.
            agent_name: The configured agent name (guarded before running).
            max_search_rounds: Max knowledge-base searches per turn before a no_match handoff.
            turn_budget_seconds: Wall-clock seconds one whole turn may take before it hands off.
            logger: A structured logger for turn events.

        Returns:
            None.

        Example:
            >>> ClassificationTurnService(gateway, kb_source, "clasification-agent", 6, 120.0, logger)  # doctest: +SKIP
        """
        self._foundry_agent_gateway = foundry_agent_gateway  # Store the Foundry gateway             # keep gateway
        self._knowledge_source = knowledge_source  # Store the in-process candidate source           # keep kb source
        self._agent_name = agent_name  # Store the agent name (config guard)                         # keep agent name
        self._max_search_rounds = max_search_rounds  # Store the per-turn search budget              # keep budget
        self._turn_budget_seconds = turn_budget_seconds  # Store the per-turn wall-clock budget      # keep time budget
        self._logger = logger  # Store the structured logger                                         # keep logger

    # ------------------------------------------ Public API ------------------------------------------
    def handle_turn_json(self, payload: dict[str, Any]) -> dict[str, Any]:  # Dict-in / dict-out boundary  # dict boundary
        """Handle one turn from a raw dict payload and return a serialisable dict.

        Args:
            payload: The turn input (conv_id + message).

        Returns:
            A serialisable dict (see AgentEntryResponse), including conv_id.

        Raises:
            pydantic.ValidationError: If the payload is missing 'conv_id' / 'message' or malformed.

        Example:
            >>> service.handle_turn_json({"conv_id": "conv_abc", "message": "outlook crashes"})  # doctest: +SKIP
            {'conv_id': 'conv_abc', 'status': 'follow_up', ...}
        """
        request = AgentEntryRequest.model_validate(payload)  # Parse/validate the payload into the request model  # validate body
        return self.handle_turn(request).model_dump()  # Process the turn and serialise the response  # run + dump

    def handle_turn(self, request: AgentEntryRequest) -> AgentEntryResponse:  # Core per-turn entry point  # core entry
        """Process one user turn against the Foundry agent, with the in-process candidate feed.

        Args:
            request: The validated turn input (conv_id + message).

        Returns:
            An AgentEntryResponse describing a follow-up, resolution, no_match, or error.

        Example:
            >>> service.handle_turn(AgentEntryRequest(conv_id="conv_abc", message="hi"))  # doctest: +SKIP
            AgentEntryResponse(conv_id='conv_abc', status='follow_up', ...)
        """
        # conv_id is the STABLE Foundry conversation id that holds this conversation's history.
        conversation_id = request.conv_id  # Foundry conversation id supplied by the calling application  # conv id
        turn_log_id = conversation_id or generate_correlation_id()  # Log key = conv_id, or a minted id  # log key

        # Guard: the agent must be configured before any turn can run.
        if not self._agent_name:  # No agent name means the deployment is misconfigured              # config guard
            self._logger.log(  # Log the misconfiguration                                            # log misconfig
                event="foundry_agent_not_configured",  # Event name                                  # event
                correlation_id=turn_log_id,  # Log key                                               # log key
                level="ERROR",  # Severity level                                                     # level
            )
            return self._build_error_response(conversation_id)  # Safe error response                # safe error

        # Guard: a turn cannot run without the conversation that carries its history.
        if not conversation_id:  # The calling application owns and must supply the conversation id  # conv guard
            self._logger.log(  # Log the rejection                                                   # log reject
                event="foundry_conv_id_required",  # Event name                                      # event
                correlation_id=turn_log_id,  # Log key (minted; no conversation id available)        # log key
                level="ERROR",  # Severity level                                                     # level
            )
            return self._build_error_response(  # Reject: conv_id is mandatory                       # reject
                None,  # No conversation id to echo                                                  # no conv id
                "No conversation id (conv_id) was provided. The calling application must supply the conv_id.",  # Message  # message
            )

        try:  # Guard the whole turn so no exception escapes to the caller                           # turn guard
            self._logger.log(event="foundry_turn_start", correlation_id=turn_log_id)  # Turn start   # log start
            return self._run_turn_loop(conversation_id, request.message, turn_log_id)  # Run the loop  # run loop
        except (FoundryAgentError, KnowledgeBaseSourceError) as known_error:  # Expected failure modes  # known error
            self._logger.log(  # Log the known failure                                               # log known
                event="foundry_turn_failed",  # Event name                                           # event
                correlation_id=turn_log_id,  # Log key                                               # log key
                level="ERROR",  # Severity level                                                     # level
                error_type=type(known_error).__name__,  # Exception class name                       # error type
                error_message=str(known_error),  # Exception message                                  # error msg
            )
            return self._build_error_response(conversation_id)  # Safe error response                # safe error
        except Exception as unexpected_error:  # Any unexpected failure -> safe fallback (never leak)  # catch-all
            self._logger.log(  # Log the unexpected failure                                          # log unexpected
                event="foundry_turn_error",  # Event name                                            # event
                correlation_id=turn_log_id,  # Log key                                               # log key
                level="ERROR",  # Severity level                                                     # level
                error_type=type(unexpected_error).__name__,  # Exception class name                  # error type
                error_message=str(unexpected_error),  # Exception message                             # error msg
            )
            return self._build_error_response(conversation_id)  # Safe error response                # safe error

    # ------------------------------------------ Search loop -----------------------------------------
    def _run_turn_loop(  # Agent + in-process candidate feed loop within a stable conversation      # search loop
        self, conversation_id: str, message: str, turn_log_id: str  # Conversation id, user text, log key  # loop args
    ) -> AgentEntryResponse:
        """Run the agent, feeding the candidate set on any search request, until a terminal reply.

        What this does:
            On an agent 'search' request this feeds the candidate set back and lets the AGENT
            choose; it loops until follow_up / resolved / no_match, the search budget is spent, or
            the wall-clock budget for the turn expires.

        Why the wall-clock budget exists:
            The caller is a synchronous chat turn with a user waiting on it. Bounding only the
            individual calls would still allow several long calls plus their retries to add up to
            a wait no user would tolerate, so the whole turn carries a deadline as well.

        Args:
            conversation_id: The stable Foundry conversation id carrying history.
            message: The user's message for this turn.
            turn_log_id: The correlation id used as the log key for this turn.

        Returns:
            An AgentEntryResponse for a follow-up, resolution, or no_match.

        Example:
            >>> service._run_turn_loop("conv_abc", "outlook crashes", "cid")  # doctest: +SKIP
            AgentEntryResponse(status='resolved', ...)
        """
        searches_done = 0  # How many knowledge-base searches we have run this turn                  # search count
        input_text = message  # First input is the user's message; later inputs are candidates / nudges  # next input
        conv_id = conversation_id  # The STABLE conversation id echoed back to the caller            # echo id
        deadline = time.monotonic() + self._turn_budget_seconds  # Wall-clock instant this turn must finish by  # deadline

        # Hard iteration backstop = search budget + a little headroom for decide/nudge turns.
        for _iteration_index in range(self._max_search_rounds + 3):  # Bounded loop (never infinite)  # bounded loop
            if time.monotonic() >= deadline:  # The turn has used its whole wall-clock budget        # out of time?
                self._logger.log(  # Log that the budget, not the agent, ended the turn              # log timeout
                    event="foundry_turn_budget_exceeded",  # Event name                              # event
                    correlation_id=turn_log_id,  # Log key                                           # log key
                    level="WARNING",  # Severity level                                               # level
                    searches_done=searches_done,  # How far the turn got before the deadline         # searches
                )
                return self._handoff_response(conv_id, None)  # Hand off rather than keep the user waiting  # handoff

            reply_text = self._foundry_agent_gateway.create_response(  # Ask the agent within the conversation  # call agent
                input_text,  # The user message, fed-back candidates, or a nudge                     # input
                conversation_id,  # The conversation carrying history server-side (stable)           # conv id
                turn_log_id,  # Correlation id for logs                                              # log key
            )
            structured_output = self._parse_agent_output(reply_text, turn_log_id)  # Parse its strict-JSON reply  # parse reply

            if structured_output is None:  # Unparseable reply -> treat as a follow-up (safe, non-leaking)  # parse fail
                self._logger.log(event="foundry_reply_parse_fallback", correlation_id=turn_log_id, level="WARNING")  # Log it  # log fallback
                cleaned_text = _JSON_FENCE_PATTERN.sub("", reply_text).strip()  # Best-effort clean of the raw text  # clean text
                return self._follow_up_response(conv_id, cleaned_text or "Could you tell me a bit more?")  # Follow-up  # follow-up

            if structured_output.status == "search":  # The agent wants a knowledge-base search      # search branch
                if not structured_output.query:  # Malformed search (no query) -> fall back to a follow-up  # no query
                    self._logger.log(event="foundry_search_without_query", correlation_id=turn_log_id, level="WARNING")  # Log  # log no query
                    return self._follow_up_response(  # Keep the conversation open                   # follow-up
                        conv_id, structured_output.agent_message or "Could you tell me a bit more?"
                    )
                if searches_done >= self._max_search_rounds:  # Search budget spent -> nudge the agent to decide  # budget hit
                    self._logger.log(event="foundry_search_budget_reached", correlation_id=turn_log_id, level="WARNING")  # Log  # log budget
                    input_text = (  # Instruct the agent to conclude with what it already has        # nudge text
                        "You have reached the maximum number of knowledge-base searches. Based on the candidates you "
                        "already have, either resolve with a kb_id or return no_match with a short summary of the "
                        "user's issue."
                    )
                    continue  # Loop once more so the agent can produce a terminal reply             # loop again
                candidates = self._knowledge_source.search(  # Search for what the agent asked about  # run search
                    structured_output.query,  # The agent's own search description for this round   # query
                    turn_log_id,  # Correlation id for logs                                          # log key
                )
                searches_done += 1  # Count this search against the budget                           # count search
                self._logger.log(  # Log the search we performed                                     # log search
                    event="foundry_kb_search_performed",  # Event name                               # event
                    correlation_id=turn_log_id,  # Log key                                           # log key
                    round=searches_done,  # Which search round this was                              # round
                    result_count=len(candidates),  # How many candidates we fed back                 # result count
                )
                input_text = self._format_candidates(structured_output.query, candidates)  # Feed candidates back  # feed back
                continue  # Loop: send the candidates to the agent to decide                         # loop again

            # A resolution is only an answer if it names an article. Without one there is nothing
            # for the caller to act on, so the turn is handed off instead.
            if structured_output.status == "resolved" and not structured_output.kb_id:  # No article named  # empty resolve
                self._logger.log(  # Record that the resolution carried no article                   # log empty
                    event="foundry_resolved_without_kb_id",  # Event name                            # event
                    correlation_id=turn_log_id,  # Log key                                           # log key
                    level="WARNING",  # Severity level                                                # level
                )
                return self._handoff_response(conv_id, structured_output.summary)  # Hand off        # handoff

            # Terminal reply: follow_up / resolved / no_match.
            return self._build_response_from_output(structured_output, conv_id)  # Map to a response  # terminal reply

        # Backstop exhausted without a terminal reply -> safe handoff.
        self._logger.log(event="foundry_turn_loop_exhausted", correlation_id=turn_log_id, level="WARNING")  # Log it  # log exhausted
        return self._handoff_response(conv_id, None)  # Close with a human handoff                   # handoff

    # ------------------------------ Reply parsing / response building -------------------------------
    def _format_candidates(self, query: str, candidates: list[dict[str, Any]]) -> str:  # Render candidates for the agent  # render candidates
        """Render the candidate articles as the next input message for the agent.

        What this does:
            Serialises the candidate list into the KB_SEARCH_RESULTS block the prompt tells the
            agent to expect, so the AGENT selects.

        Args:
            query: The search description that produced these candidates.
            candidates: The candidate article records.

        Returns:
            A text block containing the candidate articles as JSON.

        Example:
            >>> service._format_candidates("outlook crashes", [])  # doctest: +SKIP
            'KB_SEARCH_RESULTS for query "outlook crashes" ... []'
        """
        return (  # Build a clear text block the prompt tells the agent to expect                    # build block
            f'KB_SEARCH_RESULTS for query "{query}" '
            f"(JSON list of candidate articles; decide follow_up / resolved / no_match from these):\n"
            f"{json.dumps(candidates or [], ensure_ascii=False)}"
        )

    def _build_response_from_output(  # Map a terminal AgentStructuredOutput to an AgentEntryResponse  # map terminal
        self, structured_output: AgentStructuredOutput, conv_id: Optional[str]  # Parsed terminal output + conversation id  # map args
    ) -> AgentEntryResponse:
        """Map a terminal agent reply (follow_up/resolved/no_match) to the response.

        Args:
            structured_output: The parsed terminal agent output.
            conv_id: The conversation id (echoed + log key).

        Returns:
            A validated AgentEntryResponse.

        Example:
            >>> service._build_response_from_output(out, "conv_abc")  # doctest: +SKIP
            AgentEntryResponse(status='resolved', ...)
        """
        if structured_output.status == "resolved":  # Valid resolution with a kb_id (already guarded upstream)  # resolved
            self._logger.log(  # Log the resolution                                                  # log resolved
                event="foundry_kb_article_returned",  # Event name                                   # event
                correlation_id=conv_id,  # Log key                                                   # log key
                kb_id=structured_output.kb_id,  # The resolved article id                            # kb id
            )
            return AgentEntryResponse(  # Return the matched article and close the chat               # build resolved
                conv_id=conv_id,  # Echo the conversation id                                         # echo id
                status="resolved",  # Resolved                                                       # status
                agent_message=structured_output.agent_message,  # Confirmation text                  # message
                kb_id=structured_output.kb_id,  # Matched article id                                 # kb id
                summary=structured_output.summary,  # The user's issue, as the agent summarised it   # summary
                chat_close=True,  # A resolution closes the chat                                     # close chat
            )

        if structured_output.status == "no_match":  # No article found -> human handoff, close the chat  # no_match
            self._logger.log(event="foundry_no_match_handoff", correlation_id=conv_id)  # Log it      # log no_match
            return AgentEntryResponse(  # Close with a handoff message; carry the issue summary, no article  # build no_match
                conv_id=conv_id,  # Echo the conversation id                                         # echo id
                status="no_match",  # No article found                                               # status
                agent_message=structured_output.agent_message,  # Polite handoff text                # message
                kb_id=None,  # No article (enforced null)                                            # no article
                summary=structured_output.summary,  # Summary of the user's issue                    # summary
                chat_close=True,  # A no_match closes the chat                                       # close chat
            )

        # Otherwise: a follow-up turn.
        self._logger.log(event="foundry_follow_up_asked", correlation_id=conv_id)  # Log the follow-up  # log follow-up
        return self._follow_up_response(conv_id, structured_output.agent_message)  # Keep the conversation open  # follow-up

    def _follow_up_response(self, conv_id: Optional[str], agent_message: str) -> AgentEntryResponse:  # Build a follow-up  # follow-up builder
        """Build a follow-up response (conversation stays open, no article).

        Args:
            conv_id: The conversation id (echoed + log key).
            agent_message: The follow-up question / message to show the user.

        Returns:
            A follow-up AgentEntryResponse.

        Example:
            >>> service._follow_up_response("conv_abc", "Tell me more?")  # doctest: +SKIP
            AgentEntryResponse(status='follow_up', ...)
        """
        return AgentEntryResponse(  # Assemble the follow-up response                                 # build follow-up
            conv_id=conv_id,  # Echo the conversation id (pass back next turn)                        # echo id
            status="follow_up",  # Awaiting more info                                                 # status
            agent_message=agent_message or "Could you tell me a bit more?",  # The follow-up text or a default  # message
            kb_id=None,  # No article yet                                                             # no article
            summary=None,  # No summary yet                                                           # no summary
            chat_close=False,  # Conversation stays open                                              # keep open
        )

    def _handoff_response(self, conv_id: Optional[str], summary: Optional[str]) -> AgentEntryResponse:  # Build a handoff  # handoff builder
        """Build a no_match handoff response used when the turn ends without a decision.

        What this method is:
            - The single builder for every "we stopped before the agent decided" exit: the search
              budget, the wall-clock budget, the iteration backstop, and a rejected fabricated id.

        Why this exists:
            - Those exits must all look identical to the caller. Building them in one place is what
              guarantees none of them can accidentally emit an article id.

        Security and production notes:
            1. kb_id is always None here - an unverified or absent article id is never surfaced.

        Args:
            conv_id: The conversation id (echoed + log key).
            summary: Any issue summary the agent produced, or None when there is none.

        Returns:
            A no_match AgentEntryResponse that closes the conversation.

        Example:
            >>> service._handoff_response("conv_abc", "Outlook crashes on send.")  # doctest: +SKIP
            AgentEntryResponse(status='no_match', ...)
        """
        return AgentEntryResponse(  # Close with a human-handoff no_match                             # build handoff
            conv_id=conv_id,  # Echo the conversation id                                              # echo id
            status="no_match",  # No article was settled on                                           # status
            agent_message=_NO_MATCH_FALLBACK_MESSAGE,  # Polite handoff text                          # message
            kb_id=None,  # No article                                                                 # no article
            summary=summary,  # Keep any issue summary the agent produced                             # summary
            chat_close=True,  # End the conversation                                                  # close chat
        )

    def _parse_agent_output(self, reply_text: str, correlation_id: str) -> Optional[AgentStructuredOutput]:  # Strict parse  # parse output
        """Parse the agent's reply into an AgentStructuredOutput, or None on failure.

        Tolerant by design: takes the FIRST JSON object in the reply and ignores any
        prose before it or extra objects after it. This defends against the agent
        emitting more than one JSON object in a single turn.

        Args:
            reply_text: The agent's reply text.
            correlation_id: The correlation id used as the log key.

        Returns:
            A validated AgentStructuredOutput, or None if not valid contract JSON.

        Example:
            >>> service._parse_agent_output('{"status":"search","query":"x","agent_message":""}', "s")  # doctest: +SKIP
            AgentStructuredOutput(status='search', ...)
        """
        cleaned_text = _JSON_FENCE_PATTERN.sub("", reply_text).strip()  # Remove any code fences and trim  # clean text
        parsed_value = self._extract_first_json_object(cleaned_text)  # Take ONLY the first JSON object  # first object
        if parsed_value is None:  # No decodable JSON object found in the reply                       # no json
            self._logger.log(  # Log the parse failure (keyed by conv_id)                             # log parse fail
                event="foundry_reply_parse_failed",  # Event name                                     # event
                correlation_id=correlation_id,  # Log key                                             # log key
                level="WARNING",  # Severity level                                                    # level
                error_type="JSONDecodeError",  # No valid JSON object present                         # error type
                error_message="No JSON object found in the agent reply.",  # Detail                   # error msg
            )
            return None  # Signal a parse failure to the caller                                       # signal none
        try:  # Validate the extracted object against the strict output schema                        # validate
            return AgentStructuredOutput.model_validate(parsed_value)  # Validate against the contract  # model validate
        except (ValidationError, TypeError) as parse_error:  # Object present but wrong shape         # bad shape
            self._logger.log(  # Log the validation failure detail (keyed by conv_id)                 # log validate fail
                event="foundry_reply_parse_failed",  # Event name                                     # event
                correlation_id=correlation_id,  # Log key                                             # log key
                level="WARNING",  # Severity level                                                    # level
                error_type=type(parse_error).__name__,  # Exception class name                        # error type
                error_message=str(parse_error),  # Exception message                                   # error msg
            )
            return None  # Signal a parse failure to the caller                                       # signal none

    def _extract_first_json_object(self, text: str) -> Optional[dict[str, Any]]:  # First top-level JSON object in text  # extract json
        """Return the first top-level JSON object found in the text, or None.

        Uses raw_decode from the first '{' so any trailing content (a second object,
        stray prose) after a complete object is ignored.

        Args:
            text: The text that should contain a JSON object.

        Returns:
            The first decoded JSON object, or None if none is decodable.

        Example:
            >>> service._extract_first_json_object('{"a": 1}{"b": 2}')  # doctest: +SKIP
            {'a': 1}
        """
        start_index = text.find("{")  # Locate the first opening brace                                # find brace
        if start_index == -1:  # No object present at all                                             # no brace
            return None  # Nothing to decode                                                          # signal none
        try:  # Decode a single JSON value starting at the first brace                                # decode one
            parsed_object, _end_index = json.JSONDecoder().raw_decode(text[start_index:])  # First object only  # raw decode
        except json.JSONDecodeError:  # The text from the first brace is not a valid object           # decode fail
            return None  # Signal no decodable object                                                 # signal none
        if not isinstance(parsed_object, dict):  # The contract requires a JSON object                # type check
            return None  # Reject arrays/scalars                                                      # signal none
        return parsed_object  # Return the first decoded object                                       # return object

    def _build_error_response(  # Build a safe error response (status='error', conversation kept open)  # error builder
        self, conv_id: Optional[str], agent_message: str = _SAFE_FALLBACK_MESSAGE  # Conversation id + optional message  # error args
    ) -> AgentEntryResponse:
        """Build a safe error response.

        Args:
            conv_id: The conversation id when known, else None.
            agent_message: The human-facing message (defaults to the generic fallback).

        Returns:
            An AgentEntryResponse describing the error.

        Example:
            >>> service._build_error_response("conv_abc")  # doctest: +SKIP
            AgentEntryResponse(status='error', ...)
        """
        return AgentEntryResponse(  # Assemble the safe error response                                 # build error
            conv_id=conv_id,  # Echo the conversation id when known                                   # echo id
            status="error",  # Mark the turn as errored                                               # status
            agent_message=agent_message,  # The human-facing message                                  # message
            kb_id=None,  # No article resolved                                                        # no article
            summary=None,  # No summary                                                               # no summary
            chat_close=False,  # Keep the conversation open                                            # keep open
        )


# ========================================== Build / cache =========================================
def _build_turn_service(foundry_client: Any, turn_budget_seconds: float) -> ClassificationTurnService:  # Compose the service  # composition root
    """Build the turn service, the Foundry gateway and the in-process knowledge-base source.

    What this does:
        Reads and validates this agent's configuration, builds the cross-cutting services it needs
        (logging, cost tracking), and wires the gateway and knowledge-base source around the
        Foundry client the hosting application injected.

    Why the Foundry client is a parameter:
        The connection is the application's, not this agent's. Taking it as an argument is what
        keeps this agent from opening a second credential and a second client for the same project.

    Args:
        foundry_client: The application's Foundry client, exposing an `openai` Responses client.
        turn_budget_seconds: Wall-clock seconds one whole turn may take before it hands off.

    Returns:
        A fully wired ClassificationTurnService.

    Example:
        >>> isinstance(_build_turn_service(foundry_client, 120.0), ClassificationTurnService)  # doctest: +SKIP
        True
    """
    settings = load_settings()  # Load + validate this agent's section of the application config     # load config

    # Build only the cross-cutting services this agent needs.
    emitter = None  # Default: no Event Hub emitter                                                   # no emitter
    if settings.event_hub.enabled:  # Only build an emitter when Event Hub is enabled in config       # emitter toggle
        emitter = EventHubLogEmitter(  # Construct the Event Hub log emitter                          # build emitter
            fully_qualified_namespace=settings.event_hub.fully_qualified_namespace,  # Namespace from config  # namespace
            event_hub_name=settings.event_hub.event_hub_name,  # Event Hub name from config          # hub name
        )
    log_factory = LogFactory(log_level=settings.logging.log_level, emitter=emitter)  # Structured-logger factory  # log factory
    cost_tracker = CostTracker(  # Cost tracker for response token usage                              # cost tracker
        prices=settings.cost.prices,  # Per-model pricing from config                                 # prices
        logger=log_factory.get_logger("usage_cost_tracker"),  # Dedicated cost logger                # cost logger
    )

    foundry_config = settings.foundry  # Foundry agent config section                                 # foundry cfg

    foundry_agent_gateway = FoundryAgentGateway(  # Build the instrumented Foundry gateway            # build gateway
        foundry_client=foundry_client,  # The application's authenticated Foundry client             # foundry client
        agent_name=foundry_config.agent_name,  # Pre-created agent name                              # agent name
        agent_version=foundry_config.agent_version,  # Concrete version to pin, or blank             # agent version
        request_timeout_seconds=foundry_config.request_timeout_seconds,  # Per-call timeout          # timeout
        cost_tracker=cost_tracker,  # Cost tracker for response token usage                          # cost tracker
        agent_model_name=foundry_config.agent_model_name,  # Model name for cost lookup/logging      # model name
        log_factory=log_factory,  # Logger factory for the gateway                                   # log factory
        retry_max_attempts=settings.retry.max_attempts_for("foundry_agent"),  # Per-operation retry attempts  # retries
        retry_base_delay_seconds=settings.retry.base_delay_seconds,  # Backoff base delay            # base delay
        retry_max_delay_seconds=settings.retry.max_delay_seconds,  # Backoff max delay               # max delay
    )

    credentials = load_servicenow_credentials()  # Read the connection details from the environment  # credentials
    search_config = settings.servicenow_search  # Non-secret call policy from the YAML               # search cfg

    # A token is either requested from the identity provider as needed, or supplied directly.
    if credentials.uses_oauth:  # The four token settings are present                                # request one?
        token_provider = ClientCredentialsTokenProvider(  # Obtain tokens and hold them              # build provider
            token_url=credentials.oauth_token_url,  # Token endpoint                                 # token url
            client_id=credentials.oauth_client_id,  # The application's own identifier               # client id
            client_secret=credentials.oauth_client_secret,  # The application's own secret           # client secret
            scope=credentials.oauth_scope,  # The scope a token is requested for                     # scope
            request_timeout_seconds=search_config.request_timeout_seconds,  # Per-call timeout       # timeout
            verify_tls=search_config.verify_tls,  # Certificate verification                         # verify tls
            log_factory=log_factory,  # Logger factory for the token provider                        # log factory
            retry_max_attempts=settings.retry.max_attempts_for("servicenow_token"),  # Attempt cap   # retries
            retry_base_delay_seconds=settings.retry.base_delay_seconds,  # Backoff base delay        # base delay
            retry_max_delay_seconds=settings.retry.max_delay_seconds,  # Backoff ceiling             # max delay
        )
    else:  # A token was supplied directly                                                           # supplied
        token_provider = StaticTokenProvider(credentials.bearer_token)  # Present it on every request  # build provider

    search_client = ServiceNowSearchClient(  # Build the pooled search client                        # build client
        base_url=credentials.base_url,  # Endpoint URL                                               # base url
        token_provider=token_provider,  # Supplies the token for each request                        # token provider
        registration_id=credentials.registration_id,  # Identifies this integration                  # registration
        user_id=credentials.user_id,  # The account the search runs as                               # user id
        req_type=credentials.req_type,  # Fixed query parameter                                      # req type
        search_type=credentials.search_type,  # Fixed query parameter                                # search type
        request_timeout_seconds=search_config.request_timeout_seconds,  # Per-call timeout           # timeout
        pool_maxsize=search_config.pool_maxsize,  # Connections kept alive for reuse                 # pool size
        verify_tls=search_config.verify_tls,  # Certificate verification                             # verify tls
        log_factory=log_factory,  # Logger factory for the search client                             # log factory
        retry_max_attempts=settings.retry.max_attempts_for("servicenow_search"),  # Attempt cap      # retries
        retry_base_delay_seconds=settings.retry.base_delay_seconds,  # Backoff base delay            # base delay
        retry_max_delay_seconds=settings.retry.max_delay_seconds,  # Backoff ceiling                 # max delay
    )

    knowledge_base_config = settings.knowledge_base  # Limits applied to every search result         # kb cfg
    knowledge_source = KnowledgeBaseSource(  # Build the knowledge-base source                       # build kb source
        search_client=search_client,  # Performs one search per request                              # search client
        log_factory=log_factory,  # Logger factory for the knowledge-base source                     # log factory
        max_candidates=knowledge_base_config.max_candidates,  # Most candidates one search returns   # candidate cap
    )

    return ClassificationTurnService(  # Assemble and return the turn service                         # build service
        foundry_agent_gateway=foundry_agent_gateway,  # Instrumented Foundry gateway                  # gateway
        knowledge_source=knowledge_source,  # In-process candidate source                             # kb source
        agent_name=foundry_config.agent_name,  # Agent name (config guard)                            # agent name
        max_search_rounds=foundry_config.max_search_rounds,  # Per-turn search budget                # budget
        turn_budget_seconds=turn_budget_seconds,  # Per-turn wall-clock budget                       # time budget
        logger=log_factory.get_logger("classification_turn_service"),  # Turn-service logger          # logger
    )


def get_turn_service(foundry_client: Any, turn_budget_seconds: float) -> ClassificationTurnService:  # Cached accessor  # cache accessor
    """Return the cached turn service, building it once per worker (thread-safe).

    What this does:
        Builds the turn service on the first call and returns that same instance for the life of
        the worker process, so the gateway and the knowledge-base index are constructed once.

    Why the arguments only matter on the first call:
        The hosting application builds one Foundry client per worker and derives the timeout from
        its own configuration, so every caller passes the same values. They are accepted per call
        because the agent object that supplies them is constructed per request.

    Args:
        foundry_client: The application's Foundry client, exposing an `openai` Responses client.
        turn_budget_seconds: Wall-clock seconds one whole turn may take before it hands off.

    Returns:
        The process-wide ClassificationTurnService singleton.

    Example:
        >>> get_turn_service(client, 120.0) is get_turn_service(client, 120.0)  # doctest: +SKIP
        True
    """
    global _CACHED_TURN_SERVICE  # Refer to the module-level cache                                    # global cache
    if _CACHED_TURN_SERVICE is None:  # Fast path: avoid taking the lock once built                   # fast path
        with _TURN_SERVICE_LOCK:  # Serialise construction so concurrent cold requests build only once  # take lock
            if _CACHED_TURN_SERVICE is None:  # Re-check inside the lock (another thread may have built it)  # double-check
                _CACHED_TURN_SERVICE = _build_turn_service(foundry_client, turn_budget_seconds)  # Build once  # build once
    return _CACHED_TURN_SERVICE  # Return the cached turn service                                     # return cached
