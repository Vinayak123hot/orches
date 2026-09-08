"""Repositories: all SQL for the conversations, sessions, transcript and trace tables.

    base.py            BaseRepository -- shared connection, commit and transaction()
    conversations.py   conversations + conversation_turns: flow state, job state, transcript
    sessions.py        sessions: the chat list in the frontend's left panel
    agent_calls.py     agent_calls: the internal trace of every agent call
    api_requests.py    api_requests: one row per HTTP request we serve

Each repository is constructed with an open connection and owns the queries for one table
group. Holding the connection is the reason these are classes -- it stops every call site
threading `conn` (and usually `user_id`) through as parameters, and it gives the
transaction boundary somewhere to live.

The SELECT column lists are kept as class constants so the dialect variants stay in sync,
and the full statements are assembled from them at class level -- never with an f-string
at the execute() call -- so static analysis (Snyk) doesn't flag SQL built by string
formatting. Those constants are fixed literals, never user input; every user value is
bound with ?.

COMMITS: a write method calls self._commit(), which commits immediately when used on its
own but defers to the enclosing transaction() block when there is one. The service layer
wraps the sequences that must agree with each other -- completing a job, and persisting a
chat turn -- so those commit exactly once instead of two or three times. The two
trace tables are the exception and say so in their own docstrings.

ConversationRepository.advance_after_job() is the other half of that: it folds the job
outcome and the new stage into ONE conditional UPDATE, which doubles as the
compare-and-swap that stops two concurrent pollers both completing the same job.

Imported from here rather than from the individual modules, so a caller writes
`from app.db.repositories import ConversationRepository` and a later split of one module
does not touch its callers.
"""
from app.db.repositories.agent_calls import AgentCallRepository
from app.db.repositories.api_requests import ApiRequestRepository
from app.db.repositories.base import BaseRepository
from app.db.repositories.conversations import ConversationRepository
from app.db.repositories.sessions import SessionRepository

__all__ = [
    "AgentCallRepository",
    "ApiRequestRepository",
    "BaseRepository",
    "ConversationRepository",
    "SessionRepository",
]
