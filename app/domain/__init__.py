"""The business rules: what this product DOES, independent of how it is delivered.

    flow.py       SupportFlow -- the stage machine that advances a conversation
    jobs.py       JobRunner -- which agent runs a job kind, and its retry semantics
    run_state.py  what a device job's report means (pure functions)

NO FRAMEWORK, NO DATABASE, NO HTTP STATUS CODES in this package. It raises the domain
errors from core/errors.py and returns plain values; the api layer decides what each error
becomes, and the services layer decides what gets written down. That is what makes the
flow callable from a request, from the reaper's background sweep, and from a test with no
web server -- three callers, one set of rules.

The layer above (services/) sequences these: run the flow, translate the reply, persist
the turn, dispatch the job. The layer below (agents/, db/) does the actual work. Nothing
here reaches sideways into api/.

flow.py is deliberately one long module rather than a package. It is a single state
machine whose handlers each return the next stage, and following a transition across
files costs more than the file length saves.
"""
