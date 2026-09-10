"""What the orchestrator hands every agent -- and nothing about how an agent works.

Each agent is a class in agents/<name>/main.py. The orchestrator builds it once (see
app/deps.py) and calls its methods with real parameters: the SAME values that used to be
fields in the HTTP body posted to that agent's Function App. Everything BEHIND those
methods -- Foundry, Graph/Intune, ServiceNow, a KB search, prompts, retries -- belongs to
the team that owns the agent and lives in that agent's folder.

This module deliberately contains no SDK code. It is the seam, not an implementation:
putting a Foundry or Graph call here would be the orchestrator guessing at seven other
teams' internals, and every team would then have to work around the guess.

WHAT THE BASE CLASS PROVIDES
  * the collaborators the orchestrator injects -- see __init__;
  * traced(), which records one row in the agent_calls table for a call;
  * _call(), the one hook an agent team implements. It receives the request as keyword
    arguments, so "what do I get?" has the same answer it had over HTTP: the payload.

WHY THE PUBLIC METHODS LIVE ON OUR SIDE. Wherever the flow depends on an ERROR POLICY --
ServiceNowAgent.update_incident must not break a live conversation, MultilingualAgent
must hand back the original text rather than raise, SecondClassificationAgent must return
{"success": false} rather than raise -- that policy is written in the public method,
wrapped around _call. The orchestrator keeps the promises its flow is built on, and an
agent team implements exactly one method.
"""
from app.core.tracing import NULL_TRACE


class Agent:
    """Base for the in-process agents: the injected collaborators, and the _call hook."""

    #: Written to agent_calls.agent and used in log lines. Subclasses set it.
    label = "agent"

    def __init__(
        self, *, settings=None, foundry=None, timeout=None, trace=None,
        log_factory=None,
    ):
        # The app's parsed configuration (app/core/config.py). Read whatever your agent needs
        # from it, or read your own environment variables in your folder -- an agent's
        # configuration is its own business.
        self._settings = settings
        # The process-wide Azure AI Foundry client (app/clients/foundry.py), built lazily
        # and shared. Use it if your agent talks to Foundry: one client per process means
        # ONE Entra handshake per worker, where a client per agent would mean seven.
        self._foundry = foundry
        # Seconds this agent should allow for one call, from Settings. Passed in so the
        # per-turn timeout budget is set in one place rather than seven.
        self._timeout = timeout
        # This request's CallTrace, or NULL_TRACE. Never None, so no agent needs to check.
        self._trace = trace if trace is not None else NULL_TRACE
        # A structured logger named after this agent's label -- so its lines carry the
        # same name that appears in agent_calls.agent, and Splunk and SQL agree. Use it as
        #     self._logger.log(event="kb_lookup_failed", correlation_id=conv_id,
        #                      error_type=type(exc).__name__)
        # correlation_id is the conversation_id: it is what ties this line to every other
        # line of the same conversation, across the flow and the other agents.
        #
        # NEVER put a user email, free text, raw device output or an exception MESSAGE in
        # a field -- these records can be forwarded out of the tenant. Ids, numbers, enums
        # and error_type only. Pass exc_info=True and the traceback goes to our stdout
        # without going to Event Hub.
        self._logger = (
            log_factory.get_logger(self.label) if log_factory is not None else None
        )

    # -----------------------------------------------------------------------
    # For the agent implementations
    # -----------------------------------------------------------------------
    def traced(self, request):
        """Time one call and record its agent_calls row. Use as a context manager:

            with self.traced({"kb_id": kb_id}) as call:
                answer = ...                # your call to Foundry / Graph / anything
                call.response = answer      # what gets stored (optional)

        The row is written whether the block returns or raises, so a failure is recorded
        with its error rather than vanishing. Wrap only the OUTBOUND call: what the row
        should mean is "how long the agent took", not how long our own parsing took.

        Rows are per request and are written to the table by TurnTimer in services/timing.py. An
        agent that never calls this simply has no rows -- which is the honest answer, but
        it also means its latency is missing from every chart, so please use it.
        """
        return self._trace.call(self.label, request)

    def _call(self, **payload):
        """THE HOOK EACH AGENT TEAM IMPLEMENTS, in agents/<name>/main.py.

        `payload` is the request -- the fields that used to be the JSON body of the POST
        to this agent's Function App. Return what that app used to return (see the
        contract in your agent's module docstring); raise on failure, and the public
        method above applies the error policy the flow expects.
        """
        raise NotImplementedError(
            f"{type(self).__name__}._call is not implemented yet: the {self.label} "
            "agent's own code goes in its folder. It receives the fields that used to be "
            f"the HTTP request body -- here: {sorted(payload)}"
        )
