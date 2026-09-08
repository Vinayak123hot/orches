"""The rows collected for the agent_calls table during one request.

The measuring happens where the outbound call is actually made -- inside each agent, via
the `traced()` helper on agents/base.py. That is the one place every call passes through,
so no `trace` argument has to be threaded through step(), six stage handlers and a dozen
agent methods.

A CallTrace is created per request in deps.py and handed to that request's agents. It is
never shared: request 1's rows must not land in request 2's list.

WHAT CHANGED WHEN THE AGENTS CAME IN-PROCESS. This module used to hold a URL -> agent
name map, because the only thing an outbound POST knew about its destination was the URL
(minus the ?code= key). An agent is now a class that knows its own name, so it passes
`Agent.label` directly -- a row can no longer come out as "unknown" because a setting was
blank or a query string was not stripped.

`call()` is a context manager rather than a decorator or a wrapper object because the
call it measures has to be able to FAIL: the agents that swallow their own failures
(MultilingualAgent.detect returns the previous language, ServiceNowAgent.update_incident
logs and returns, SecondClassificationAgent returns {"success": false}) do so ABOVE this
layer, so the row is written from inside the `except` before their handling ever runs.
"""
import json
import time
from contextlib import contextmanager

from app.core.config import logger

MAX_FIELD = 8000  # cap per column: one runaway script must not bloat the table


class AgentCall:
    """The row of a call that is still in flight.

    The caller assigns `response` once it has one; if the call raises first, the row is
    still written with whatever was set (usually nothing) plus the error.
    """

    __slots__ = ("response",)

    def __init__(self):
        self.response = None


class CallTrace:
    """The rows collected during ONE request. deps.py builds a fresh one per request."""

    def __init__(self):
        self.rows = []

    @contextmanager
    def call(self, agent: str, request):
        """Time one agent call and add its row, whether the call returns or raises.

        perf_counter is monotonic, so an NTP adjustment mid-call cannot produce a
        negative duration, and it is read immediately after the call returns -- our own
        JSON parsing and DB writes stay out of the number, which is what makes it
        honestly "how long the agent took".
        """
        record = AgentCall()
        started = time.perf_counter()
        try:
            yield record
        except Exception as exc:
            self.record(agent, request, record.response, _ms(started), exc)
            raise  # unchanged: the caller's own error handling still sees the original
        self.record(agent, request, record.response, _ms(started))

    def record(self, agent, request, response, duration_ms, error=None):
        """Add one row.

        Best-effort: this runs on the live request path, so a failure while recording
        must never turn a healthy agent call into a failed one.
        """
        try:
            self.rows.append(
                {
                    "agent": agent,
                    "request_text": _field(request),
                    "response_json": _field(response),
                    "duration_ms": duration_ms,
                    "is_error": error is not None,
                    "error_text": _field(_error_text(error)),
                }
            )
        except Exception:
            logger.exception("could not record an agent call (row dropped)")

    def drop_last(self):
        """Drop the row just added -- used by the job status poll.

        A job runs 3-15 minutes and is polled every 30s, so ~30 of its 31 rows would say
        "still running". Only the final poll carries the outcome.
        """
        if self.rows:
            self.rows.pop()


class NullTrace:
    """A trace that keeps nothing, for callers with no request to attribute rows to.

    The agents hold a trace unconditionally and call it on every request, so this is what
    makes `if self._trace is not None` unnecessary in nine modules. Used by tests, which
    construct an agent with no trace at all.
    """

    rows = ()

    @contextmanager
    def call(self, agent, request):
        yield AgentCall()

    def record(self, *args, **kwargs):
        pass

    def drop_last(self):
        pass


NULL_TRACE = NullTrace()


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _error_text(error):
    """Describe a failure, naming the ORIGINAL cause where one was chained.

    The agents translate SDK exceptions into UpstreamUnavailable (see agents/base.py), so
    without the `__cause__` every failed row would read "UpstreamUnavailable: <agent>
    call failed" and the actual reason -- a timeout, an auth failure, a 429 -- would live
    only in the logs.
    """
    if error is None:
        return None
    if isinstance(error, str):
        return error
    text = f"{type(error).__name__}: {error}"
    cause = getattr(error, "__cause__", None)
    if cause is not None:
        text += f" (caused by {type(cause).__name__}: {cause})"
    return text


def _field(value):
    """Serialize a value for storage and cap its length."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= MAX_FIELD else text[:MAX_FIELD] + "...[truncated]"
