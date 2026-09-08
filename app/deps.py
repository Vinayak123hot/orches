"""Dependency wiring -- the one place objects are constructed.

Two lifetimes:

  PROCESS-WIDE, built once at import: the database factory and the lazily built Foundry
  client. These are exactly what must be reused across requests -- an AIProjectClient does
  an Entra handshake, so building one per request would add that handshake to every turn,
  and building one per agent would multiply it by seven.

  PER-REQUEST, built by the Depends providers: the repositories, the agents, the flow and
  the services. The repositories because each is bound to one DB connection that must be
  closed when the response is sent (get_db is a generator dependency, so FastAPI runs the
  `finally` for us). The agents because each carries this request's CallTrace, and request
  1's rows must not land in request 2's list -- they are handed the shared Foundry client
  above, so building them costs nothing but a few references.

THIS IS THE COMPOSITION ROOT. Every agent, service and repository takes its collaborators
as constructor arguments and never reaches for a module global, so this is the only module
that knows how the app is assembled -- and a test can build any piece directly with fakes
and never import this file. It is also where "which class provides the orchestrator agent"
is answered: nothing above here names an agent module.

The pooled requests.Sessions that used to live here are gone with the HTTP hop. The
agents are in-process now (see agents/__init__.py); an agent that makes its own outbound
calls owns its own pooling, and is given HTTP_POOL_MAXSIZE through `settings` to size it.
"""
from fastapi import Depends

from app.agents.classification_first.main import FirstClassificationAgent
from app.agents.classification_second.main import SecondClassificationAgent
from app.agents.diagnostics.main import DiagnosticsAgent
from app.agents.multilingual.main import MultilingualAgent
from app.agents.orchestrator.main import OrchestratorAgent
from app.agents.servicenow.main import ServiceNowAgent
from app.agents.troubleshoot.main import TroubleshootAgent
from app.clients.foundry import FoundryClient
from app.core.config import settings
from app.core.tracing import CallTrace
from app.db.database import Database
from app.db.repositories import (
    AgentCallRepository,
    ApiRequestRepository,
    ConversationRepository,
    SessionRepository,
)
from app.domain.flow import SupportFlow
from app.domain.jobs import JobRunner
from app.services import ChatService, HistoryService, JobService

# ---------------------------------------------------------------------------
# Process-wide singletons
# ---------------------------------------------------------------------------
database = Database(settings)

# The Azure AI Foundry client: one per worker process, built on the FIRST call that needs
# it and reused for the life of the process. Every agent is handed this same object rather
# than making its own, so the credential handshake happens once and the agents stay cheap
# to construct per request.
foundry_client = FoundryClient(
    settings.AZURE_FOUNDRY_PROJECT_ENDPOINT,
    timeout=settings.FOUNDRY_HTTP_TIMEOUT,
    max_retries=settings.FOUNDRY_MAX_RETRIES,
)


# ---------------------------------------------------------------------------
# Per-request providers
# ---------------------------------------------------------------------------
def get_db():
    """Open a connection for this request and close it once the response is sent."""
    conn = database.connect()
    try:
        yield conn
    finally:
        conn.close()


def get_conversation_repo(conn=Depends(get_db)) -> ConversationRepository:
    return ConversationRepository(conn, use_sqlite=database.use_sqlite)


def get_session_repo(conn=Depends(get_db)) -> SessionRepository:
    return SessionRepository(conn, use_sqlite=database.use_sqlite)


def get_agent_call_repo(conn=Depends(get_db)) -> AgentCallRepository:
    return AgentCallRepository(conn)


def get_api_request_repo(conn=Depends(get_db)) -> ApiRequestRepository:
    return ApiRequestRepository(conn)


def _agents_for(trace: CallTrace):
    """This request's agents, wrapped in the flow and the job runner that use them.

    Cheap to build. Each agent is a handful of references -- the Foundry client is passed
    in, so nothing here connects, authenticates or pools anything. What must be
    per-request is the TRACE: request 1's rows must not land in request 2's list, and
    since an agent holds its trace, the agent is per-request too. SupportFlow holds no
    state of its own, so building one is six assignments.

    Every agent gets the same three collaborators (config, the Foundry client, the trace)
    and its call budget, so adding an agent is one line here and one folder in agents/.
    An agent uses what it needs and ignores the rest -- what it talks to is its own
    business, and the point of this seam is that the orchestrator does not need to know.
    """
    shared = {
        "settings": settings,
        "foundry": foundry_client,
        "timeout": settings.AGENT_HTTP_TIMEOUT,
        "trace": trace,
    }
    lang = MultilingualAgent(default_lang=settings.DEFAULT_LANG, **shared)
    # Which class runs which kind of job is settled here and nowhere else; the flow only
    # ever names a KIND (see constants.JOB_KIND_*).
    jobs = JobRunner(
        diagnostics=DiagnosticsAgent(**shared),
        troubleshoot=TroubleshootAgent(**shared),
        trace=trace,
    )
    flow = SupportFlow(
        orchestrator=OrchestratorAgent(**shared),
        classify_first=FirstClassificationAgent(**shared),
        classify_second=SecondClassificationAgent(**shared),
        servicenow=ServiceNowAgent(**shared),
        multilingual=lang,
        settings=settings,
    )
    return flow, lang, jobs


def get_chat_service(
    conversations: ConversationRepository = Depends(get_conversation_repo),
    sessions: SessionRepository = Depends(get_session_repo),
    agent_calls: AgentCallRepository = Depends(get_agent_call_repo),
    api_requests: ApiRequestRepository = Depends(get_api_request_repo),
) -> ChatService:
    trace = CallTrace()
    flow, lang, jobs = _agents_for(trace)
    return ChatService(
        conversations=conversations,
        sessions=sessions,
        flow=flow,
        foundry=foundry_client,
        jobs=jobs,
        multilingual=lang,
        agent_calls=agent_calls,
        trace=trace,
        api_requests=api_requests,
    )


def get_job_service(
    conversations: ConversationRepository = Depends(get_conversation_repo),
    agent_calls: AgentCallRepository = Depends(get_agent_call_repo),
    api_requests: ApiRequestRepository = Depends(get_api_request_repo),
) -> JobService:
    trace = CallTrace()
    flow, lang, jobs = _agents_for(trace)
    return JobService(
        conversations=conversations,
        flow=flow,
        jobs=jobs,
        multilingual=lang,
        settings=settings,
        agent_calls=agent_calls,
        trace=trace,
        api_requests=api_requests,
    )


def get_history_service(
    conversations: ConversationRepository = Depends(get_conversation_repo),
    sessions: SessionRepository = Depends(get_session_repo),
) -> HistoryService:
    return HistoryService(conversations=conversations, sessions=sessions)


# ---------------------------------------------------------------------------
# Non-HTTP entry point
# ---------------------------------------------------------------------------
def make_job_service(conn) -> JobService:
    """Build a JobService from a bare connection, with no FastAPI involved.

    The Depends providers above only work inside a request. The reaper's background sweep
    (workers/reaper.py) has none, so it needs this -- and being able to add it at all is
    the point of keeping the service layer free of HTTP: the sweep calls exactly the same
    code the endpoint does.

    The reaper gets its own trace per sweep, so a job it completes is recorded like any
    other -- otherwise the calls that finish abandoned jobs would be the ones missing
    from the table.

    api_requests is deliberately NOT passed. That table is "requests we served", and a
    reaper sweep is not one -- nobody was waiting on it. Feeding sweeps in would inflate
    the row count with traffic no user ever generated and skew every latency percentile
    built on it.
    """
    trace = CallTrace()
    flow, lang, jobs = _agents_for(trace)
    return JobService(
        conversations=ConversationRepository(conn, use_sqlite=database.use_sqlite),
        flow=flow,
        jobs=jobs,
        multilingual=lang,
        settings=settings,
        agent_calls=AgentCallRepository(conn),
        trace=trace,
    )
