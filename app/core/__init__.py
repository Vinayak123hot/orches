"""Cross-cutting foundations: configuration, constants, errors, tracing.

    config.py     Settings (env / App Service Application Settings) + the shared logger
    constants.py  domain constants: stages, agent vocabularies, fixed user-facing lines
    errors.py     the framework-free error types raised below the HTTP layer
    tracing.py    CallTrace -- the agent_calls rows collected during one request

THE BOTTOM OF THE DEPENDENCY GRAPH. Every other package imports this one; this one
imports none of them. If something here ever needs `api`, `services`, `domain`, `db` or
`agents`, it is in the wrong folder -- that import would turn the graph into a cycle and
leave nothing to build the rest on.

The test for "does this belong in core?" is whether it would still make sense with the
web layer, the database and the agents all deleted. Settings, the stage names, the error
types and the timing rows all pass. Anything that knows what a conversation *is* does
not -- that is domain/.
"""
