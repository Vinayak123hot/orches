"""Service layer: one class per use-case group, each owning a whole unit of work.

    chat.py      ChatService -- starting and continuing conversations
    jobs.py      JobService -- advancing a running diagnostics/troubleshoot job
    history.py   HistoryService -- the read-only queries
    timing.py    TurnTimer -- measures one served request, writes both timing tables
    payloads.py  the response dicts every return path shares

Sits between api/ (HTTP) and domain/ (the rules). A service method owns the SEQUENCE of a
use case -- run the flow, translate the reply, persist the turn, dispatch any async job --
while knowing nothing about requests, responses or status codes. It returns plain dicts
and raises the framework-free errors in core/errors.py; the api layer is the only place
that turns those into HTTP.

Each service takes its collaborators in __init__ (repositories, agents, the flow) and
never reaches for a module global. That is what makes them testable: a test builds
ChatService(FakeConversationRepo(), FakeSessionRepo(), FakeFlow(), FakeFoundry()) and
drives any branch with no database, no Azure and no network.

TRANSACTION BOUNDARY: this layer owns it. Each service method does its slow work first
(flow handlers, agent calls, translation) and only then opens repo.transaction() around
the writes that must agree with each other -- so a crash can't leave the conversation row
advanced with its transcript rows missing, and no agent call is ever inside an open
transaction holding row locks.

Imported from here rather than from the individual modules, so a caller writes
`from app.services import ChatService`.
"""
from app.services.chat import ChatService
from app.services.history import HistoryService
from app.services.jobs import JobService
from app.services.timing import TurnTimer

__all__ = ["ChatService", "HistoryService", "JobService", "TurnTimer"]
