####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# THE ENTRY POINT of the classification agent - the one function the calling application invokes.  #
#   1. classify(conversation_id, message) advances one classification turn and returns a dict.     #
#   2. Borrow the application's Foundry connection and its per-call timeout; open nothing new.     #
#   3. Delegate the turn to the cached turn service and map its outcome to the caller's shape.     #
#   4. Record the call for tracing, and raise on a failure so the caller's error policy applies.   #
#                                                                                                  #
# CONTRACT                                                                                         #
#     request   conversation_id, message                                                           #
#     response  still gathering:  {"chat_close": false, "kb_id": null, "summary": null,            #
#                                  "agent_message": "<the next question to ask>",                  #
#                                  "status": "follow_up"}                                          #
#               article found:    {"chat_close": true, "kb_id": "KB0024755",                       #
#                                  "summary": "<the user's issue, for the next agent>",            #
#                                  "agent_message": "<safe confirmation line>",                    #
#                                  "status": "resolved"}                                           #
#               no article:       {"chat_close": true, "kb_id": null,                              #
#                                  "summary": "<the user's issue>",                                #
#                                  "agent_message": "<handoff line>", "status": "no_match"}        #
#                                                                                                  #
# THIS AGENT OWNS THE FOLLOW-UP LOOP. The caller does not count questions or cap them: it shows    #
# `agent_message`, stays on the classification stage, and calls again with the user's reply until  #
# `chat_close` is true. Adding or removing a question is a change to this agent, not to the flow.  #
#                                                                                                  #
# READ `status` BEFORE ACTING ON `chat_close`. A closed turn is either "resolved" (kb_id is set    #
# and the conversation can move on) or "no_match" (kb_id is null and the conversation must be      #
# handed to the support team). Treating a no_match as a resolution would pass a null kb_id down    #
# the chain, so route on `status` and use `kb_id` only when it is "resolved".                      #
#                                                                                                  #
# Source:-                                                                                         #
#   - app.agents.base supplies Agent (the injected collaborators and the _call hook).              #
#   - app.core.errors supplies UpstreamTransient (raised so the caller's error policy applies).    #
#   - turn_orchestrator supplies get_turn_service (the cached, process-wide turn service).         #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of type annotations (PEP 563)     # future import

from app.agents.base import Agent  # Base class carrying the injected collaborators and the _call hook  # agent base
from app.core.errors import UpstreamTransient  # Domain error raised when a turn cannot be completed  # domain error

from .turn_orchestrator import get_turn_service  # Cached service that drives one classification turn  # service factory

# Wall-clock seconds one turn may take when the caller injects no timeout of its own.
_DEFAULT_TURN_BUDGET_SECONDS = 120.0  # Fallback budget for one whole classification turn            # default budget


# ======================================= Classification agent =====================================
class FirstClassificationAgent(Agent):  # Turns a described problem into an article id plus a summary
    """Turns a described problem into a knowledge-base article id plus a summary.

    What this class is:
        - The agent's public surface. It holds the collaborators the calling application injects
          (its configuration, its Foundry connection, the per-call timeout, this request's trace)
          and exposes one operation: advance the classification conversation by one turn.

    Why this exists:
        - To keep the calling application's flow talking to a small, stable method signature while
          everything behind it - Foundry, the knowledge base, prompts, retries, budgets - stays
          inside this folder and can change without touching the flow.

    Security and production notes:
        1. This agent opens no connection of its own: the Foundry client it uses is the one the
           application already built and authenticated, so there is one credential handshake per
           worker process.
        2. Every outbound call is recorded through the injected trace, so a failure is stored with
           its cause rather than disappearing.
        3. A turn that cannot be completed raises rather than returning a normal-looking answer,
           so the caller applies its own error policy instead of showing the user a dead end.
    """

    #: Written to the call trace and used in log lines.
    label = "classify_1"  # Stable label identifying this agent in traces and logs                   # trace label

    def classify(self, conversation_id: str, message: str) -> dict:  # Advance classification by one turn
        """Advance classification by one turn. Returns the agent's response dict.

        What this method is:
            - THE ENTRY POINT. The calling application invokes this with the conversation id it
              owns and the user's message for this turn, and receives the dict described in the
              contract at the top of this file.

        Why conversation_id is required:
            - Each answer only means something against the questions already asked. That history
              lives on the Foundry conversation identified by this id rather than being re-sent, so
              the same id must be passed on every turn of one conversation.

        Security and production notes:
            1. `message` is untrusted user text and is passed through as data, never as instructions.
            2. The call is timed and recorded through the injected trace, including when it fails.

        Args:
            conversation_id: The stable Foundry conversation id carrying this conversation.
            message: The user's message for this turn.

        Returns:
            A dict with chat_close, kb_id, summary, agent_message and status (see the contract).

        Raises:
            UpstreamTransient: If the turn could not be completed. The conversation itself is
                intact, so the caller may show its fallback and let the user resend.

        Example:
            >>> agent.classify("conv_abc123", "Outlook keeps crashing on send")  # doctest: +SKIP
            {'chat_close': False, 'kb_id': None, 'summary': None, 'agent_message': '...', 'status': 'follow_up'}
        """
        return self._call(conversation_id=conversation_id, message=message)  # Run the turn through the hook  # run turn

    # ------------------------------------------ Internals -------------------------------------------
    def _call(self, **payload) -> dict:  # Run one turn against the turn service and shape the result
        """Run one classification turn and map its outcome to the caller's dict.

        What this method is:
            - The implementation behind classify(): it obtains the process-wide turn service, runs
              one turn inside the call trace, and translates the turn service's outcome into the
              contract the calling application reads.

        Why the turn service is fetched here rather than held on the instance:
            - This agent object is built per request, while the turn service, its gateway and the
              loaded knowledge-base index must live for the whole worker process. Fetching the
              cached service per call keeps the expensive parts process-wide and this object cheap.

        Security and production notes:
            1. An errored turn is converted into a raised domain error. The turn service never
               raises by design, so without this the caller could not tell a genuine follow-up
               question apart from a failure and would loop asking the user for more detail.
            2. Only the outcome fields are returned; nothing internal to the search loop is exposed.

        Args:
            **payload: The turn's fields - conversation_id and message.

        Returns:
            A dict with chat_close, kb_id, summary, agent_message and status.

        Raises:
            UpstreamTransient: If the turn service reports that the turn could not be completed.

        Example:
            >>> agent._call(conversation_id="conv_abc", message="hi")  # doctest: +SKIP
            {'chat_close': False, ...}
        """
        conversation_id = payload["conversation_id"]  # The Foundry conversation carrying the history  # conv id
        message = payload["message"]  # The user's message for this turn                             # user message

        turn_service = get_turn_service(  # The cached, process-wide turn service                    # get service
            foundry_client=self._foundry,  # The application's already-authenticated Foundry client  # foundry client
            turn_budget_seconds=float(self._timeout or _DEFAULT_TURN_BUDGET_SECONDS),  # Budget for one whole turn  # time budget
        )

        with self.traced({"conversation_id": conversation_id, "message": message}) as call:  # Time + record the call  # trace call
            outcome = turn_service.handle_turn_json(  # Run one turn (never raises; reports status)  # run turn
                {"conv_id": conversation_id, "message": message}  # The turn input                   # turn input
            )
            call.response = outcome  # Store what came back on the trace row                         # record response

        status = outcome.get("status")  # The outcome kind: follow_up / resolved / no_match / error  # read status
        if status == "error":  # The turn could not be completed                                     # errored?
            raise UpstreamTransient(  # Raise so the caller applies its own error policy             # raise domain
                "The classification agent could not complete this turn."  # Cause-free, safe message  # error message
            )

        return {  # Shape the outcome into the contract the calling application reads                # build result
            "chat_close": bool(outcome.get("chat_close")),  # True once this conversation is finished  # close flag
            "kb_id": outcome.get("kb_id"),  # The matched article id; None unless status is 'resolved'  # article id
            "summary": outcome.get("summary"),  # The user's issue, for whatever runs next           # issue summary
            "agent_message": outcome.get("agent_message") or "",  # The line that is safe to show the user  # user text
            "status": status,  # Which outcome this is - route on this, not on chat_close alone      # outcome kind
        }
