####################################################################################################
# Project name      : IT Support Orchestrator API -- Azure App Service (FastAPI)                   #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# THE COMPOSITION ROOT: the one module that knows how this app is assembled.                       #
#   1. Build the three PROCESS-WIDE singletons: the DB factory, the Foundry client, the logger.    #
#   2. Provide the PER-REQUEST objects via Depends: connection, repositories, agents, services.    #
#   3. _agents_for(): construct this request's seven agents around its own CallTrace.              #
#   4. make_job_service(): the same JobService, from a bare connection, with no FastAPI at all.    #
#                                                                                                  #
# Source:-                                                                                         #
#   - fastapi.Depends declares the per-request graph and runs get_db's `finally` for us.           #
#   - The seven app.agents.*.main modules are named HERE and nowhere else above this file.         #
#   - app.clients.foundry / app.db / app.event_hub supply the three process-wide singletons.       #
#   - app.domain (SupportFlow, JobRunner) and app.services are assembled per request.              #
####################################################################################################
"""Dependency wiring -- the one place objects are constructed.

What this module is:
    - Three module-level singletons, six Depends providers, one agent factory and one
      non-HTTP entry point.

Why this exists:
    - Two lifetimes:

        PROCESS-WIDE, built once at import: the database factory and the lazily built
        Foundry client. These are exactly what must be reused across requests -- an
        AIProjectClient does an Entra handshake, so building one per request would add that
        handshake to every turn, and building one per agent would multiply it by seven.

        PER-REQUEST, built by the Depends providers: the repositories, the agents, the flow
        and the services. The repositories because each is bound to one DB connection that
        must be closed when the response is sent (get_db is a generator dependency, so
        FastAPI runs the `finally` for us). The agents because each carries this request's
        CallTrace, and request 1's rows must not land in request 2's list -- they are handed
        the shared Foundry client above, so building them costs nothing but a few
        references.

Security and production notes:
    1. THIS IS THE COMPOSITION ROOT. Every agent, service and repository takes its
       collaborators as constructor arguments and never reaches for a module global, so
       this is the only module that knows how the app is assembled -- and a test can build
       any piece directly with fakes and never import this file. It is also where "which
       class provides the orchestrator agent" is answered: nothing above here names an
       agent module.
    2. A request holds exactly ONE connection: get_db is cached per request, so every
       repository built for that request shares it. That is what makes the pool sizing in
       Settings.SQL_POOL_MAX_SIZE correct -- and it is why the two trace repositories must
       not commit inside another's transaction.
    3. The pooled requests.Sessions that used to live here are gone with the HTTP hop. The
       agents are in-process now (see agents/__init__.py); an agent that makes its own
       outbound calls owns its own pooling, and is given HTTP_POOL_MAXSIZE through
       `settings` to size it.
"""

# ============================================ Imports =============================================
from fastapi import Depends  # Declares the per-request graph; runs get_db's finally for us          # fastapi

from app.agents.classification_agent.main import FirstClassificationAgent  # issue -> kb_id          # agent
from app.agents.kb_validation_agent.main import SecondClassificationAgent  # kb_id -> how            # agent
# The agents below have no folder under app/agents/ yet. Their imports and the lines that
# build them are commented rather than deleted, so restoring one is uncommenting two lines
# once its folder lands. Until then _agents_for cannot assemble a flow -- see the note there.
# from app.agents.diagnostics.main import DiagnosticsAgent  # Diagnose on the user's device          # agent
# from app.agents.multilingual.main import MultilingualAgent  # Detect language + translate          # agent
# from app.agents.orchestrator.main import OrchestratorAgent  # Classify the user's message          # agent
# from app.agents.servicenow.main import ServiceNowAgent  # Interaction + incident lifecycle         # agent
# from app.agents.troubleshoot.main import TroubleshootAgent  # Remediate on the user's device       # agent
from app.clients.foundry import FoundryClient  # The process-wide Azure AI Foundry client            # client
from app.core.config import settings  # The parsed configuration every layer reads                   # config
from app.core.tracing import CallTrace  # One per request; never shared between requests             # tracing
from app.db.database import Database  # The connection factory                                       # db
from app.db.repositories import (  # All SQL; each repository is bound to one connection             # repos
    AgentCallRepository,  # agent_calls: the per-call trace                                          # repo
    ApiRequestRepository,  # api_requests: one row per served request                                # repo
    ConversationRepository,  # Flow state, job state and the transcript                              # repo
    SessionRepository,  # The frontend's chat list                                                   # repo
)
from app.event_hub import build_log_factory  # Structured logging, optionally to Event Hub           # logging
from app.domain.flow import SupportFlow  # The stage machine                                         # domain
from app.domain.jobs import JobRunner  # kind -> agent, plus the retry semantics                     # domain
from app.services import ChatService, HistoryService, JobService  # The three use cases              # services

# ==================================== Process-wide singletons =====================================
# The connection factory. Constructing it also configures the Azure SQL pool, which MUST
# happen before the first connection is opened -- see Database._configure_pool.
database = Database(settings)  # One per worker process, built at import                             # db factory

# The Azure AI Foundry client: one per worker process, built on the FIRST call that needs
# it and reused for the life of the process. Every agent is handed this same object rather
# than making its own, so the credential handshake happens once and the agents stay cheap
# to construct per request.
foundry_client = FoundryClient(
    settings.AZURE_FOUNDRY_PROJECT_ENDPOINT,  # Project data-plane endpoint                          # endpoint
    timeout=settings.FOUNDRY_HTTP_TIMEOUT,  # Per-request ceiling, in seconds                        # timeout
    max_retries=settings.FOUNDRY_MAX_RETRIES,  # Explicit, so the worst case is visible              # retries
)

# The structured-logging factory: one per worker process, because it owns the Event Hub
# producer and its AMQP connection. Handed to every agent and service so each gets a
# logger named after itself -- and so "where do log lines go" is answered here, once,
# rather than by each component reaching for a global.
#
# Built with emitter=None unless EVENTHUB_ENABLED is set with a namespace and hub name,
# in which case nothing leaves the process and this costs nothing. main.py closes it.
log_factory = build_log_factory(settings)  # Ships with forwarding OFF by default                    # log factory


# ===================================== Per-request providers ======================================
def get_db():
    """Open a connection for this request and close it once the response is sent.

    A generator dependency, so FastAPI runs the `finally` after the response is sent. It is
    also CACHED per request, which is what makes every repository below share one
    connection -- and therefore one transaction.
    """
    conn = database.connect()  # Raises DatabaseUnavailable (-> 503) if it cannot connect            # open conn
    try:  # The whole request runs inside this                                                       # serve
        yield conn  # Handed to every repository provider below                                      # yield conn
    finally:  # FastAPI runs this after the response has been sent                                   # always
        conn.close()  # Back to the driver's pool                                                    # close conn


def get_conversation_repo(conn=Depends(get_db)) -> ConversationRepository:
    """This request's conversation repository, on the shared connection."""
    return ConversationRepository(conn, use_sqlite=database.use_sqlite)  # Dialect for find_running  # repo


def get_session_repo(conn=Depends(get_db)) -> SessionRepository:
    """This request's session repository, on the shared connection."""
    return SessionRepository(conn, use_sqlite=database.use_sqlite)  # Dialect for list_recent        # repo


def get_agent_call_repo(conn=Depends(get_db)) -> AgentCallRepository:
    """This request's agent_calls repository. NOTE: it commits on its own -- see its docstring."""
    return AgentCallRepository(conn)  # Must be written AFTER any transaction() block                # repo


def get_api_request_repo(conn=Depends(get_db)) -> ApiRequestRepository:
    """This request's api_requests repository. NOTE: it commits on its own, like agent_calls."""
    return ApiRequestRepository(conn)  # Must be written AFTER any transaction() block               # repo


# ========================================= Agent assembly =========================================
def _agents_for(trace: CallTrace):
    """This request's agents, wrapped in the flow and the job runner that use them.

    What this function does:
        - Builds the four shared collaborators once, constructs all seven agents with them,
          and returns (flow, lang, jobs) -- the three objects the services actually hold.

    Why it exists:
        - Cheap to build. Each agent is a handful of references -- the Foundry client is
          passed in, so nothing here connects, authenticates or pools anything. What must
          be per-request is the TRACE: request 1's rows must not land in request 2's list,
          and since an agent holds its trace, the agent is per-request too. SupportFlow
          holds no state of its own, so building one is six assignments.
        - Every agent gets the same three collaborators (config, the Foundry client, the
          trace) and its call budget, so adding an agent is one line here and one folder in
          agents/. An agent uses what it needs and ignores the rest -- what it talks to is
          its own business, and the point of this seam is that the orchestrator does not
          need to know.

    Security and production notes:
        1. The trace MUST be per-request. Passing a shared one would file one user's agent
           calls against another user's request row.
        2. Which class runs which kind of job is settled in the JobRunner below and nowhere
           else; the flow only ever names a KIND (see constants.JOB_KIND_*).

    Args:
        trace: This request's CallTrace, handed to every agent and to JobRunner.

    Returns:
        (flow, lang, jobs) -- SupportFlow, MultilingualAgent, JobRunner.

    Current state:
        Only the two classification agents have folders under app/agents/. The flow, the
        language agent and the job runner each need agents that do not exist yet, so they
        are returned as None and the lines that build them are commented below, next to the
        agent each one needs. The chat and job endpoints therefore cannot serve a turn; the
        classification agent itself is exercised through its own run_local.py, which builds
        it directly and does not come through here.
    """
    shared = {  # The same four collaborators for every agent, so adding one is a single line        # shared kwargs
        "settings": settings,  # An agent reads whatever it needs, or its own env vars               # config
        "foundry": foundry_client,  # ONE Entra handshake per worker, not seven                      # foundry
        "timeout": settings.AGENT_HTTP_TIMEOUT,  # The per-turn budget, set in one place             # timeout
        "trace": trace,  # PER-REQUEST: never shared between requests                                # trace
        # Each agent's base class turns this into a logger named for its own label, so an
        # agent team writes self._logger.log(event=..., correlation_id=conv_id) and its
        # lines are already tagged with the same name that appears in agent_calls.agent.
        "log_factory": log_factory,  # Becomes a logger named for the agent's own label              # log factory
    }
    lang = None  # MultilingualAgent(default_lang=settings.DEFAULT_LANG, **shared)                   # lang agent
    # Which class runs which kind of job is settled here and nowhere else; the flow only
    # ever names a KIND (see constants.JOB_KIND_*).
    jobs = None  # JobRunner(
    #     diagnostics=DiagnosticsAgent(**shared),  # JOB_KIND_DIAGNOSTIC                             # agent
    #     troubleshoot=TroubleshootAgent(**shared),  # JOB_KIND_TROUBLESHOOT                         # agent
    #     trace=trace,  # So a "still running" poll row is dropped from OUR trace                    # trace
    # )
    flow = None  # SupportFlow(
    #     orchestrator=OrchestratorAgent(**shared),  # Decides whether this is support work          # agent
    #     classify_first=FirstClassificationAgent(**shared),  # issue -> kb_id + summary             # agent
    #     classify_second=SecondClassificationAgent(**shared),  # kb_id -> how to resolve it         # agent
    #     servicenow=ServiceNowAgent(**shared),  # Interaction + incident lifecycle                  # agent
    #     multilingual=lang,  # The SAME instance the service holds, so its state is shared          # agent
    #     settings=settings,  # For MAX_NONACTIONABLE and DEFAULT_DEVICE_ID                          # config
    # )
    return flow, lang, jobs  # The three objects the services actually hold                          # return


# ======================================= Service providers ========================================
def get_chat_service(
    conversations: ConversationRepository = Depends(get_conversation_repo),
    sessions: SessionRepository = Depends(get_session_repo),
    agent_calls: AgentCallRepository = Depends(get_agent_call_repo),
    api_requests: ApiRequestRepository = Depends(get_api_request_repo),
) -> ChatService:
    """This request's ChatService, with a fresh trace and its own agents."""
    trace = CallTrace()  # PER-REQUEST: never shared                                                 # new trace
    flow, lang, jobs = _agents_for(trace)  # Cheap: a handful of references                          # build agents
    return ChatService(
        conversations=conversations,  # Flow state and the transcript                                # repo
        sessions=sessions,  # The sidebar row and its current-conversation pointer                   # repo
        flow=flow,  # The stage machine                                                              # domain
        foundry=foundry_client,  # Only used to mint a new conversation                              # client
        jobs=jobs,  # Starts a device job and supplies its baseline stamp                            # domain
        multilingual=lang,  # Translates the outgoing lines                                          # agent
        agent_calls=agent_calls,  # Written from TurnTimer, after the transaction                    # repo
        trace=trace,  # The same trace the agents hold                                               # trace
        api_requests=api_requests,  # A browser IS waiting on this request                           # repo
        log_factory=log_factory,  # Becomes a logger named "chat_service"                            # log factory
    )


def get_job_service(
    conversations: ConversationRepository = Depends(get_conversation_repo),
    agent_calls: AgentCallRepository = Depends(get_agent_call_repo),
    api_requests: ApiRequestRepository = Depends(get_api_request_repo),
) -> JobService:
    """This request's JobService, with a fresh trace and its own agents."""
    trace = CallTrace()  # PER-REQUEST: never shared                                                 # new trace
    flow, lang, jobs = _agents_for(trace)  # Cheap: a handful of references                          # build agents
    return JobService(
        conversations=conversations,  # Job columns, stage and the transcript                        # repo
        flow=flow,  # Its two completion handlers build the reply                                    # domain
        jobs=jobs,  # Polls the agent and interprets its report                                      # domain
        multilingual=lang,  # Translates the outgoing lines                                          # agent
        settings=settings,  # The four job knobs                                                     # config
        agent_calls=agent_calls,  # Written from TurnTimer, after the transaction                    # repo
        trace=trace,  # The same trace the agents hold                                               # trace
        api_requests=api_requests,  # A browser IS waiting on this poll                              # repo
        log_factory=log_factory,  # Becomes a logger named "job_service"                             # log factory
    )


def get_history_service(
    conversations: ConversationRepository = Depends(get_conversation_repo),
    sessions: SessionRepository = Depends(get_session_repo),
) -> HistoryService:
    """This request's HistoryService. No trace and no timer: it touches no agent."""
    return HistoryService(conversations=conversations, sessions=sessions)  # Reads only              # service


# ====================================== Non-HTTP entry point ======================================
def make_job_service(conn) -> JobService:
    """Build a JobService from a bare connection, with no FastAPI involved.

    What this function does:
        - Does what get_job_service does, but taking a connection directly and omitting the
          api_requests repository.

    Why it exists:
        - The Depends providers above only work inside a request. The reaper's background
          sweep (workers/reaper.py) has none, so it needs this -- and being able to add it
          at all is the point of keeping the service layer free of HTTP: the sweep calls
          exactly the same code the endpoint does.

    Security and production notes:
        1. The reaper gets its own trace per sweep, so a job it completes is recorded like
           any other -- otherwise the calls that finish abandoned jobs would be the ones
           missing from the table.
        2. api_requests is deliberately NOT passed. That table is "requests we served", and
           a reaper sweep is not one -- nobody was waiting on it. Feeding sweeps in would
           inflate the row count with traffic no user ever generated and skew every latency
           percentile built on it.
        3. log_factory IS passed, for the opposite reason: a job the reaper finishes is the
           case you most want in Splunk, because no browser was watching it. The
           turn_completed line it writes carries endpoint="jobs_status" with no
           api_requests row alongside, which is exactly how a sweep is told apart from a
           poll.

    Args:
        conn: An open connection owned by the caller (the reaper closes it in a `finally`).

    Returns:
        A JobService that writes agent_calls and logs, but no api_requests row.

    Example:
        >>> make_job_service(conn).sweep_running_jobs()  # doctest: +SKIP
    """
    trace = CallTrace()  # PER-SWEEP: a job the reaper finishes is traced like any other             # new trace
    flow, lang, jobs = _agents_for(trace)  # Cheap: a handful of references                          # build agents
    return JobService(
        conversations=ConversationRepository(conn, use_sqlite=database.use_sqlite),  # Caller's conn  # repo
        flow=flow,  # Its two completion handlers build the reply                                    # domain
        jobs=jobs,  # Polls the agent and interprets its report                                      # domain
        multilingual=lang,  # Translates the outgoing lines                                          # agent
        settings=settings,  # The four job knobs, including the two REAPER_* ones                    # config
        agent_calls=AgentCallRepository(conn),  # Traced like any other completion                   # repo
        trace=trace,  # The same trace the agents hold                                               # trace
        log_factory=log_factory,  # Deliberately kept: nobody else was watching this job             # log factory
    )  # NOTE: no api_requests -- a sweep is not a request we served                                 # no timing
