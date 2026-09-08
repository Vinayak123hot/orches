"""The agents -- one folder each, called in-process as plain Python classes.

Every agent used to be its own Azure Function App, reached with an HTTP POST whose body
carried the arguments. They are now classes: the orchestrator holds one instance per
agent and calls a named method with real parameters, so the "request body" is just the
argument list -- and a wrong field is a TypeError at the call site instead of a KeyError
in another process.

    base.py                   Agent -- the injected collaborators and the _call hook
    orchestrator/             OrchestratorAgent         -- classify the user's message
    classification_first/     FirstClassificationAgent  -- issue -> kb_id + summary
    classification_second/    SecondClassificationAgent -- kb_id -> how to resolve it
    servicenow/               ServiceNowAgent           -- interaction + incident lifecycle
    multilingual/             MultilingualAgent         -- detect language + translate
    diagnostics/              DiagnosticsAgent          -- diagnose on the user's device
    troubleshoot/             TroubleshootAgent         -- remediate on the user's device

WHO WRITES WHAT. The class, its method signatures and its documented return shape are the
CONTRACT the orchestrator calls -- they replace what used to be an HTTP endpoint, so
changing one is a change to an interface two teams share. The body of _call, and any
number of modules beside it in the same folder, belong to the team that owns the agent:
that is where Foundry, Graph/Intune, ServiceNow, KB search, prompts and retries live.

ONE FOLDER PER AGENT, and no agent imports another. That boundary is what the separate
Function Apps used to enforce for us, and it is what keeps "who owns this" answerable now
that they share a process. Anything two agents genuinely share belongs in base.py, not in
a sideways import.

WHAT AN AGENT MAY NOT DO. An agent makes its own call and returns what it got. It does not
decide flow (app/domain/flow.py), does not touch the database, and does not interpret another
agent's result -- see app/domain/run_state.py for why the device run-state interpretation stayed with
the caller rather than moving into the two device agents.

Nothing here is constructed at import. Every agent takes its collaborators as constructor
arguments and is built in app/deps.py, so a test can drive any of them with a stub and no
network at all.
"""
