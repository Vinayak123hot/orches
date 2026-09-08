"""JobRunner: which agent runs a job kind, and what its answer is worth.

The diagnostics and troubleshoot agents used to be Function Apps behind POST /start and
GET /status; they are now classes in agents/diagnostics/ and agents/troubleshoot/, called
directly. This class is the seam between "a job kind" -- which is all the flow knows and
all it should know -- and the agent that provides it.

It does three things and no more:

  1. DISPATCH. kind -> agent, in one dict. Adding a third job type is one entry here plus
     its own folder under agents/.
  2. ERROR TRANSLATION with the retry semantics the caller depends on. UpstreamTransient
     means "ask again in 30 seconds"; UpstreamUnavailable means "this will not get better
     on its own". JobService retries the first and lets the second through, so getting
     this boundary right is what decides whether a broken agent surfaces in seconds or
     silently burns the job's 20-minute deadline.
  3. TRACE HYGIENE. A poll that says "still running" has its row dropped, because a job
     is polled every 30s for minutes and ~30 of its 31 rows would otherwise say nothing.

The interpretation of what a report MEANS lives in run_state.py, next door, because it is
pure logic and deserves to be tested as such.

NO SIMULATION MODE. There is no "pretend the job worked" switch and no fallback for an
agent that isn't ready: everything below raises. A previous version faked progress
whenever a URL was unset, which made the app tell real users "No issues found; Outlook
profile repaired" while nothing had run -- and resolve their ServiceNow incident when they
confirmed. Tests inject a scripted stand-in for this class instead.
"""
from app.core.config import logger
from app.core.constants import JOB_KIND_DIAGNOSTIC, JOB_KIND_TROUBLESHOOT
from app.core.errors import AppError, UpstreamTransient, UpstreamUnavailable
from app.core.tracing import NULL_TRACE
from app.domain import run_state


class JobRunner:
    """Routes a job kind to its agent and reports {state, message, output}."""

    def __init__(self, diagnostics, troubleshoot, trace=None):
        # Kind -> agent. The flow names a KIND (it knows what it wants done); which class
        # provides it is settled here.
        self._agents = {
            JOB_KIND_DIAGNOSTIC: diagnostics,
            JOB_KIND_TROUBLESHOOT: troubleshoot,
        }
        self._trace = trace if trace is not None else NULL_TRACE

    def _agent(self, kind: str):
        agent = self._agents.get(kind)
        if agent is None:
            # A bug, not a configuration problem: the kind is written in our own code.
            raise UpstreamUnavailable(f"no agent registered for job kind {kind!r}")
        return agent

    # -- trigger ------------------------------------------------------------
    def baseline_stamp(self) -> str:
        """Trigger-time stamp to store with the job -- see run_state.baseline_stamp."""
        return run_state.baseline_stamp()

    def start(self, kind: str, conversation_id: str, params: dict) -> str:
        """Kick off the long-running run and return its job_id (does NOT wait).

        `params` is what the flow collected for this kind -- device_id, kb_id, and either
        a summary or a script_name plus its params. It is passed as keyword arguments, so
        a field the agent does not accept is a TypeError here, at the call site, instead
        of a silently ignored key in a JSON body.

        Raises rather than inventing a job_id: a fake id would make the flow report a
        device repair that never happened.
        """
        agent = self._agent(kind)
        try:
            job_id = agent.start(conversation_id, **(params or {}))
        except AppError:
            # Already a domain error -- the agent decided whether a retry is worthwhile,
            # so don't relabel it.
            raise
        except NotImplementedError as exc:
            raise UpstreamUnavailable(f"{kind} agent is not implemented yet") from exc
        except Exception as exc:
            logger.error(
                "%s start failed conv_id=%s (%s): %s",
                kind, conversation_id, type(exc).__name__, exc,
            )
            raise UpstreamUnavailable(f"{kind} start failed: {exc}") from exc

        if not job_id or not isinstance(job_id, str):
            # The agent ran but gave us nothing to poll -- a contract violation, not a
            # blip, so it must not be retried.
            raise UpstreamUnavailable(f"{kind} start returned no job_id ({job_id!r})")
        return job_id

    # -- status -------------------------------------------------------------
    def check_status(self, kind: str, job_id: str, baseline: str = "") -> dict:
        """Ask the agent how the job is going. Returns {state, message, output}.

        `baseline` is the trigger-time timestamp (conversations.job_baseline), used to
        tell OUR fresh result apart from a stale report left by an earlier run on the
        same device.
        """
        agent = self._agent(kind)
        if not job_id:
            raise UpstreamUnavailable(f"{kind} status check has no job_id")

        try:
            data = agent.status(job_id)
        except AppError:
            raise
        except NotImplementedError as exc:
            # NOT transient: retrying three times would just delay the same answer, and
            # the poller would then report "still working" until the job expired.
            raise UpstreamUnavailable(f"{kind} agent is not implemented yet") from exc
        except Exception as exc:
            # Treated as a blip so the caller retries: this is polled every ~30s for
            # minutes, and one bad poll must cost a poll rather than the whole job.
            raise UpstreamTransient(f"{kind} status check failed: {exc}") from exc

        if not isinstance(data, dict):
            raise UpstreamUnavailable(
                f"{kind} status returned {type(data).__name__}, expected a dict"
            )
        return self._traced(run_state.derive(data, baseline))

    def _traced(self, status: dict) -> dict:
        """Keep the trace row only when the poll carries an outcome.

        The agent recorded its own row by the time we get here, so a "still running"
        answer is discarded again -- otherwise the ~30 no-news polls of every job would be
        the large majority of the agent_calls table. A FAILED poll is never dropped: it
        raised above and never reached this method.
        """
        if status.get("state") == "running":
            self._trace.drop_last()
        return status
