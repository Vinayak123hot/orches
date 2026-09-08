"""Job controller: the poll that advances a running device job."""
from fastapi import APIRouter, Depends, HTTPException

from app.api.schemas import ChatResponse
from app.core.config import logger
from app.core.errors import NotFound, UpstreamUnavailable
from app.deps import get_job_service
from app.services import JobService

router = APIRouter(tags=["jobs"])


@router.get("/jobs/status", response_model=ChatResponse)
def job_status(
    user_id: str,
    conversation_id: str,
    session_id: str = None,
    service: JobService = Depends(get_job_service),
):
    """The FE spinner polls this every ~30s to advance a running device job."""
    try:
        return service.advance_job(user_id, conversation_id, session_id)
    except (NotFound, UpstreamUnavailable):
        # UpstreamUnavailable means the diagnostics/troubleshoot agent cannot answer at
        # all -- a 502 says that, where a blanket 500 would blame us.
        raise
    except Exception:
        logger.exception("job_status failed conv_id=%s", conversation_id)
        raise HTTPException(status_code=500, detail="Failed to read job status")
