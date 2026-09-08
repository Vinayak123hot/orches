"""History controllers: the read-only session and transcript queries."""
from fastapi import APIRouter, Depends, HTTPException

from app.api.schemas import SessionListResponse, TranscriptResponse
from app.core.config import logger
from app.deps import get_history_service
from app.services import HistoryService

router = APIRouter(tags=["history"])


@router.get("/sessions/{user_id}", response_model=SessionListResponse)
def get_sessions(
    user_id: str,
    service: HistoryService = Depends(get_history_service),
):
    """List a user's most recent sessions (up to 20), newest first."""
    try:
        return service.list_sessions(user_id)
    except Exception:
        logger.exception("get_sessions failed user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Failed to load sessions")


@router.get(
    "/conversations/{user_id}/{session_id}", response_model=TranscriptResponse
)
def get_conversation(
    user_id: str,
    session_id: str,
    service: HistoryService = Depends(get_history_service),
):
    """Return the session's messages as a flat list of {role, content}."""
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    try:
        return service.get_transcript(user_id, session_id)
    except Exception:
        logger.exception("get_conversation failed user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Failed to load conversations")
