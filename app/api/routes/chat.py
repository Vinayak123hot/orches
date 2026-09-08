"""Chat controllers: starting a conversation and continuing one."""
from fastapi import APIRouter, Depends, HTTPException

from app.api.responses import (
    ended_chat_response,
    escalated_chat_response,
    fallback_chat_response,
)
from app.api.schemas import ChatRequest, ChatResponse, ContinueChatRequest
from app.core.config import logger
from app.core.errors import ConversationEnded, NotFound, TurnFailed
from app.deps import get_chat_service
from app.services import ChatService

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    service: ChatService = Depends(get_chat_service),
):
    """Start a new chat session and run the first turn.

    On a server-side failure returns the fallback message instead of a raw 500.
    """
    try:
        return service.start_chat(payload.user_id, payload.message)
    except ConversationEnded as exc:
        # Unreachable today (a new chat starts at stage=None, which always has a
        # handler). Handled the same way as in /chat/continue so the two endpoints
        # behave identically if the flow ever gains a terminal first turn.
        return ended_chat_response(payload.user_id, None, str(exc))
    except NotFound:
        raise  # client error -> 404 via the handler in main.py
    except TurnFailed as exc:
        logger.exception(
            "chat failed user_id=%s incident_id=%s", payload.user_id, exc.incident_id
        )
        if exc.incident_id:
            # A real ticket exists. Give them the number, don't invite a retry.
            return escalated_chat_response(payload.user_id, exc.incident_id)
        # Nothing was created anywhere -- no ticket, no row, nothing in the history --
        # so a retry starts genuinely fresh and is the right thing to ask for.
        return fallback_chat_response(payload.user_id)
    except Exception:
        logger.exception("chat failed user_id=%s", payload.user_id)
        return fallback_chat_response(payload.user_id)


@router.post("/chat/continue", response_model=ChatResponse)
def continue_chat(
    payload: ContinueChatRequest,
    service: ChatService = Depends(get_chat_service),
):
    """Continue an ACTIVE conversation in a session by session_id."""
    if payload.session_id is None:
        raise HTTPException(
            status_code=422, detail="session_id or conversation_id is required"
        )
    try:
        return service.continue_chat(
            payload.user_id, payload.session_id, payload.message
        )
    except ConversationEnded as exc:
        # The conversation is finished -- not a failure. Return the normal shape with
        # done=True so the FE resets to a new chat, instead of a 409 it would have to
        # special-case. See ended_chat_response.
        return ended_chat_response(payload.user_id, payload.session_id, str(exc))
    except NotFound:
        raise  # the session/conversation genuinely doesn't exist -> 404 via main.py
    except TurnFailed as exc:
        # The conversation survived -- report its real stage with done=False so the FE
        # keeps it and the user can resend the same message.
        logger.exception(
            "continue_chat failed session_id=%s stage=%s",
            payload.session_id, exc.stage,
        )
        return fallback_chat_response(
            payload.user_id,
            session_id=payload.session_id,
            conversation_id=exc.conversation_id,
            stage=exc.stage,
            done=False,
        )
    except Exception:
        logger.exception("continue_chat failed session_id=%s", payload.session_id)
        return fallback_chat_response(payload.user_id, session_id=payload.session_id)
