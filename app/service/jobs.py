"""JobService: advancing a running diagnostics/troubleshoot job (what the poll drives)."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.config import Settings, logger
from app.core.constants import (
    DIAGNOSTICS_RUNNING,
    JOB_DONE,
    JOB_FAILED,
    JOB_KIND_DIAGNOSTIC,
    JOB_KIND_TROUBLESHOOT,
    JOB_TERMINAL,
    TROUBLESHOOT_RUNNING,
)
from app.core.errors import AppError, NotFound, UpstreamTransient
from app.services.payloads import job_payload, join, visible
from app.services.timing import TurnTimer

# Cap on the raw device output we store, so one runaway script can't bloat a row.
MAX_JOB_OUTPUT = 8000


def _is_overdue(job_started_at, timeout_minutes: int) -> bool:
    """True when a job was triggered longer ago than we are willing to wait.

    Wall-clock, measured from the trigger -- which is the whole point. The previous rule
    counted polls, and a poll only happens while a browser is open, so a user who closed
    their tab never accumulated any and the conversation stayed RUNNING forever.

    A missing or unparseable timestamp returns False: better to keep waiting (the reaper
    will look again next sweep) than to fail a job that may be running perfectly well.
    """
    if not job_started_at:
        return False
    try:
        started = datetime.fromisoformat(str(job_started_at).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("unparseable job_started_at %r; not expiring", job_started_at)
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - started > timedelta(minutes=timeout_minutes)


class JobService:
    """Advancing a running diagnostics/troubleshoot job (what the FE poll drives)."""

    def __init__(
        self, conversations, flow, jobs, multilingual, settings: Settings,
        agent_calls=None, trace=None, api_requests=None,
    ):
        self._conversations = conversations
        self._flow = flow
        self._jobs = jobs
        self._lang = multilingual
        self._settings = settings
        # All three optional, so the existing tests can build a service without them.
        self._agent_calls = agent_calls
        self._trace = trace
        self._api_requests = api_requests

    def advance_job(
        self, user_id: str, conversation_id: str, session_id: Optional[str] = None
    ) -> dict:
        """The FE spinner polls this every ~30s (no backend queue/loop).

        On each poll we look up the job_id by conversation_id, ask the job's agent for its
        status LIVE, store the latest status in the DB, and:
          - still running -> return the progress with done=False (keep spinning)
          - done/failed   -> advance the flow ONCE (result + next question), persist the
                             new stage, and return done=True so the FE stops polling.

        Timed like a chat turn, and for the same reasons -- see TurnTimer. The rows are
        written AFTER the poll's own writes, so the trace is never inside their
        transaction, and from a `finally`, so a poll that failed still records why. When
        the reaper drives this instead of a browser there is no api_requests row, because
        nobody was waiting on that request (see deps.make_job_service).
        """
        timer = TurnTimer(
            "jobs_status",
            agent_calls=self._agent_calls,
            api_requests=self._api_requests,
            trace=self._trace,
        )
        failed = True
        try:
            payload = self._advance_job(user_id, conversation_id, session_id)
            failed = False
            return payload
        finally:
            timer.finish(user_id, conversation_id, failed=failed)

    def _advance_job(self, user_id, conversation_id, session_id):
        conversation = self._conversations.load(user_id, conversation_id)
        if conversation is None:
            raise NotFound("Conversation not found")
        stage = conversation.get("stage")
        flow_vars = conversation.get("vars", {})
        job = self._conversations.get_job(user_id, conversation_id)

        # Not in a running stage -> nothing to poll (not started, or already advanced).
        if stage not in (DIAGNOSTICS_RUNNING, TROUBLESHOOT_RUNNING):
            return job_payload(
                user_id, session_id, conversation_id, stage,
                done=job.get("job_status") in JOB_TERMINAL,
                messages=[job.get("job_message") or "No active job."],
            )

        # Running -> which agent to ask, from the stage. The stage names a KIND; JobRunner
        # owns the kind -> agent mapping, so this stays a two-line decision.
        kind = (
            JOB_KIND_DIAGNOSTIC if stage == DIAGNOSTICS_RUNNING
            else JOB_KIND_TROUBLESHOOT
        )

        status = self._poll_status(kind, job, conversation_id)
        state = status.get("state", "running")

        # Still running -> either keep waiting, or give up because it is overdue.
        if state not in ("done", "failed"):
            if _is_overdue(job.get("job_started_at"), self._settings.JOB_TIMEOUT_MINUTES):
                status = {
                    "state": "failed",
                    "message": (
                        f"No result after {self._settings.JOB_TIMEOUT_MINUTES} minutes. "
                        "Your ticket stays open."
                    ),
                    "output": None,
                }
                state = "failed"
            else:
                message = status.get("message") or "Working..."
                self._conversations.update_job_message(
                    user_id, conversation_id, message
                )
                return job_payload(
                    user_id, session_id, conversation_id, stage,
                    done=False, messages=[message],
                )

        return self._complete(
            user_id, conversation_id, session_id, stage, flow_vars, status, state
        )

    # -- internals ----------------------------------------------------------
    def sweep_running_jobs(self, limit: int = None) -> dict:
        """Advance every conversation still parked in a RUNNING stage.

        This is what the reaper calls. Without it a job only ever moves when a BROWSER
        polls -- so a user who closed their tab left the conversation stuck in
        DIAGNOSTICS_RUNNING forever, with a ServiceNow ticket that said "diagnostics
        started" and nothing more.

        Each conversation goes through the SAME advance_job() the FE poll uses, so there
        is one code path and one set of behaviour. If a browser and the reaper reach the
        same job together -- or two instances both sweep it -- the compare-and-swap in
        advance_after_job() lets exactly one of them win.

        One conversation failing never stops the sweep: an AppError is an expected
        problem (agent unavailable, conversation gone) and we move on; anything else is
        our bug and gets a full traceback.
        """
        limit = limit if limit is not None else self._settings.REAPER_BATCH_SIZE
        # Skip anything a browser is still polling: its last_updated_at is fresh, so it is
        # already being advanced by the user's own requests and does not need us.
        stale_before = (
            datetime.now(timezone.utc)
            - timedelta(minutes=self._settings.REAPER_STALE_MINUTES)
        ).isoformat()
        rows = self._conversations.find_running(limit, stale_before)
        advanced = still_running = failed = 0
        for row in rows:
            try:
                payload = self.advance_job(row["user_id"], row["id"])
                if payload.get("done"):
                    advanced += 1
                else:
                    still_running += 1
            except AppError as exc:
                logger.warning(
                    "sweep: skipped conv_id=%s: %s", row.get("id"), exc
                )
                failed += 1
            except Exception:
                logger.exception("sweep: BUG advancing conv_id=%s", row.get("id"))
                failed += 1
        return {
            "found": len(rows), "advanced": advanced,
            "still_running": still_running, "failed": failed,
        }

    def _poll_status(self, kind, job, conv_id) -> dict:
        """One poll's worth of status checking, retrying only what a retry can fix.

        ONLY UpstreamTransient is retried -- a blip, where the next attempt may well
        succeed. If every attempt fails we report the last known message as "still
        running", so the blip costs a poll rather than the whole job.

        Everything else is deliberately NOT caught:
          * UpstreamUnavailable -- a contract violation, or an agent that cannot answer
            at all. Retrying it three times just delays the same failure, so it surfaces
            immediately.
          * TypeError, KeyError, ... -- bugs in our own code. A blanket `except
            Exception` here used to swallow them and report "still running", so a real
            defect looked like a slow device job.
        """
        for _ in range(self._settings.JOB_STATUS_RETRIES):
            try:
                return self._jobs.check_status(
                    kind, job.get("job_id"), job.get("job_baseline") or ""
                )
            except UpstreamTransient as exc:
                logger.warning(
                    "check_status attempt failed conv_id=%s: %s", conv_id, exc
                )
        return {
            "state": "running",
            "message": job.get("job_message") or "Working...",
            "output": None,
        }

    def _complete(
        self, user_id, conversation_id, session_id, stage, flow_vars, status, state
    ) -> dict:
        """Advance the flow after a job finishes -- exactly once, and atomically.

        The order below is deliberate:

        1. Build the reply first (the flow handlers, plus translation). These call agents
           -- ServiceNow on the failure path, and the multilingual agent -- so they must
           run BEFORE any transaction is opened. An open transaction holds locks on the
           conversation row, and must never span an agent call.

        2. Then one transaction containing exactly two writes: the conditional UPDATE
           that moves the stage and records the job outcome together, and the transcript
           rows. Either both land or neither does, so a crash can no longer leave the
           stage advanced with its messages missing.

        The conditional UPDATE is also the race guard. Only the caller that still sees
        the RUNNING stage updates a row; a second poller (two browser tabs, a refresh
        mid-poll, another worker process, the reaper on another instance) gets 0 rows and
        skips -- which is what stops the same messages being written to the transcript
        twice.
        """
        done = state == "done"
        # What diagnostics_completed / troubleshoot_completed read off the job row. Built
        # from the status we already hold, so no write has to happen before this step.
        #
        # job_message is the USER-SAFE line; the raw device output is kept apart in
        # job_output and is never put into job_view, so it cannot reach a message list.
        job_view = {
            "job_status": JOB_DONE if done else JOB_FAILED,
            "job_message": status.get("message") or ("Complete." if done else "Failed."),
        }
        # Truncated so a pathological script can't write megabytes into the row.
        raw_output = status.get("output")
        job_output = str(raw_output)[:MAX_JOB_OUTPUT] if raw_output else None
        if job_output and not done:
            # The failure reason used to be discarded entirely. Record that we captured
            # it (not the text itself -- that would copy device data into the logs).
            logger.info(
                "job failed conv_id=%s; %d chars of device output stored in job_output",
                conversation_id, len(job_output),
            )

        messages: list = []
        if stage == DIAGNOSTICS_RUNNING:
            messages, new_stage, flow_vars = self._flow.diagnostics_completed(
                conversation_id, flow_vars, messages, job_view
            )
        else:  # TROUBLESHOOT_RUNNING
            messages, new_stage, flow_vars = self._flow.troubleshoot_completed(
                conversation_id, flow_vars, messages, job_view
            )
        messages = visible(self._lang.translate_messages(messages, flow_vars.get("lang")))
        answer = join(messages)

        repo = self._conversations
        with repo.transaction():
            won = repo.advance_after_job(
                user_id, conversation_id,
                expected_stage=stage, new_stage=new_stage, flow_vars=flow_vars,
                question="[job-poll]", answer=answer,
                job_status=job_view["job_status"],
                job_message=job_view["job_message"],
                job_output=job_output,
            )
            if won:
                # Job completion produces assistant-only lines (result + next question).
                # No user turn here -- the FE was polling, not chatting.
                repo.append_turns(
                    user_id, conversation_id, [("assistant", m) for m in messages]
                )

        if won:
            return job_payload(
                user_id, session_id, conversation_id, new_stage,
                done=True, messages=messages, answer=answer,
            )

        # We lost the race. Report what the winner persisted, not our own copy, so both
        # pollers agree on what the user is looking at.
        logger.info(
            "job completion already claimed by another poller conv_id=%s", conversation_id
        )
        return self._already_advanced(user_id, conversation_id, session_id)

    def _already_advanced(self, user_id, conversation_id, session_id) -> dict:
        """Payload for a poller that lost the completion race: re-read and report."""
        conversation = self._conversations.load(user_id, conversation_id) or {}
        job = self._conversations.get_job(user_id, conversation_id)
        return job_payload(
            user_id, session_id, conversation_id, conversation.get("stage"),
            done=True,
            messages=[job.get("job_message") or "Complete."],
        )
