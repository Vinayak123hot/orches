"""The app's routers, aggregated into one.

    chat.py      POST /chat, POST /chat/continue
    jobs.py      GET  /jobs/status
    history.py   GET  /sessions/{user_id}, GET /conversations/{user_id}/{session_id}

main.py includes `router` below under Settings.API_PREFIX, so every path here is served
beneath /api. One module per group of endpoints, so adding an endpoint touches one file
and the set of endpoints is readable from this list.
"""
from fastapi import APIRouter

from app.api.routes import chat, history, jobs

router = APIRouter()
router.include_router(chat.router)
router.include_router(jobs.router)
router.include_router(history.router)


@router.get("/")
def read_root():
    """Service banner at the prefix root (/api/).

    Kept for the callers and smoke checks that already use it -- the frontend's only
    call today is this one. The App Service health probe uses /health instead -- see
    main.py -- because a probe should not depend on the prefix, and this endpoint's path
    moves with it.
    """
    return {"message": "IT Support Orchestrator API"}
