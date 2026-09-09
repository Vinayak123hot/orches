####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# Pydantic contracts for the two boundaries one classification turn crosses.                       #
#   1. AgentEntryRequest    - the validated turn input ({conv_id, message}).                       #
#   2. AgentStructuredOutput - the strict per-turn JSON the Foundry agent emits (internal shape).  #
#   3. AgentEntryResponse   - the validated turn outcome the turn service returns.                 #
#   (Knowledge-base records are handled as raw dicts and are intentionally not modelled here.)     #
#                                                                                                  #
# Note on the caller-facing shape:                                                                 #
#   AgentEntryResponse is the TURN SERVICE's contract, not the orchestrator's. main.py maps it to  #
#   the dict the support flow reads; keep the two in step when either changes.                     #
#                                                                                                  #
# Source:-                                                                                         #
#   - from __future__ import annotations enables postponed evaluation of type annotations.         #
#       - annotations:- lets forward references in field types resolve lazily (PEP 563).           #
#   - from typing import Literal, Optional supplies the field typing helpers.                      #
#       - Literal:- pins the status field to a fixed set of allowed string values.                 #
#       - Optional:- marks fields that may be null (query, kb_id, summary).                        #
#   - from pydantic import BaseModel is the base class for every boundary model here.              #
#       - BaseModel:- validates / serialises the request, agent-output and response shapes.        #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of annotations (PEP 563)          # future import

from typing import Literal, Optional  # Typing helpers for fixed choices and optional values        # typing helpers

from pydantic import BaseModel  # Pydantic base model for validation / serialisation                # pydantic base


# ==================================== Turn boundary models ========================================
class AgentEntryRequest(BaseModel):  # The validated input for one classification turn
    """The validated input accepted for one classification turn.

    A conversation is identified by `conv_id` - the Foundry conversation id. The calling
    application creates the conversation and owns that id; it is required on every turn
    so Foundry supplies the running history. The Foundry conversation holds that state.

    What this model is:
        - The request contract at the agent boundary: exactly the two values a turn needs
          ({conv_id, message}).

    Why this exists:
        - To validate the inbound values once, at the edge, so the turn service always
          receives a well-typed {conv_id, message} instead of raw, untrusted arguments.

    Security and production notes:
        1. `message` is untrusted user text - never interpolate it into prompts / queries
           without the downstream guards; treat it as data, not instructions.
        2. `conv_id` selects whose history is read, so it must come from the calling
           application's own conversation record and never from user input.

    Example:
        {"conv_id": "conv_abc123", "message": "Outlook keeps crashing on send"}
    """

    conv_id: str  # Foundry conversation id supplied by the calling application                     # conversation id
    message: str  # The user's message for this turn                                                # user message


class AgentStructuredOutput(BaseModel):  # The strict JSON the Foundry agent emits per turn
    """The strict JSON object the Foundry agent returns as its message each turn.

    The agent's system prompt requires exactly these fields. 'search' is an INTERNAL
    status: the agent asks the turn service to run a knowledge-base search and feed the
    candidates back; it is never returned to the caller. The other statuses are surfaced.

    What this model is:
        - The per-turn output contract of the Foundry agent - the machine-readable JSON
          the turn service parses to decide whether to search, ask, resolve or hand off.

    Why this exists:
        - To force the agent's free-form reply into a strict, validated shape so the
          service can branch on `status` deterministically instead of scraping prose.

    Security and production notes:
        1. `status == "search"` and its `query` are INTERNAL - never surface either to
           the user; only follow_up / resolved / no_match are shown.
        2. `agent_message` must NEVER mention a knowledge-base article; `kb_id` is an
           internal routing key, not user-facing - enforce this when mapping to the response.

    Example:
        {"status": "search", "query": "outlook desktop crashes on send",
         "agent_message": "", "kb_id": null, "summary": null, "chat_close": false}
        {"status": "resolved", "agent_message": "Thank you for the details - I'm working on this now.",
         "kb_id": "KB0024755", "summary": "Outlook crashes on send.", "chat_close": true}
    """

    status: Literal["search", "follow_up", "resolved", "no_match"]  # Search, asking, resolved, or handoff  # turn status
    agent_message: str  # Human-facing text; never names an article. Empty for 'search'             # user text
    query: Optional[str] = None  # For status 'search': the search description to run               # internal query
    kb_id: Optional[str] = None  # Resolved article id (None unless status == 'resolved')           # routing key
    summary: Optional[str] = None  # Resolved OR no_match: the USER'S own issue, lightly polished   # user issue
    chat_close: bool = False  # True when resolved OR no_match (conversation ends); False otherwise  # end flag


class AgentEntryResponse(BaseModel):  # The validated outcome of one classification turn
    """The validated outcome the turn service returns for one turn.

    `conv_id` is echoed back so the caller can confirm which conversation the outcome
    belongs to; it is also the key the structured logs are stored under.

    What this model is:
        - The response contract at the turn-service boundary: the caller-facing subset of the
          agent's outcome, with the internal 'search' status already resolved away.

    Why this exists:
        - To give the calling code one stable, validated shape per turn and to keep internal
          signals (raw search turns, article text) out of what is handed back.

    Security and production notes:
        1. `agent_message` never mentions an article; on error it carries a safe fallback
           string only - never a stack trace or exception detail.
        2. `kb_id` is an internal routing key (not shown to the user) and is always None
           for no_match; `summary` is the user's own issue, never the article text.

    Example:
        {"conv_id": "conv_abc123", "status": "resolved",
         "agent_message": "Thank you for the details - I'm working on this now.",
         "kb_id": "KB0024755", "summary": "Outlook crashes on send.", "chat_close": true}
    """

    conv_id: Optional[str] = None  # Foundry conversation id - echoed back; also the log key         # conversation id
    status: Literal["follow_up", "resolved", "no_match", "error"]  # Asking, resolved, handoff, or safe error  # response status
    agent_message: str  # Human-facing message; never names an article. Safe fallback on error      # user text
    kb_id: Optional[str] = None  # Resolved article id; None until resolved (always None for no_match)  # routing key
    summary: Optional[str] = None  # Resolved OR no_match: the USER'S own issue, lightly polished    # user issue
    chat_close: bool = False  # Whether the conversation has ended (defaults to False)               # end flag
