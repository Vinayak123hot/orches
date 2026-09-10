"""ChatService: starting and continuing conversations (the command side)."""
import uuid

from app.core.constants import DONE
from app.core.errors import ConversationEnded, NotFound, TurnFailed
from app.services.payloads import chat_payload, join, visible
from app.services.timing import TurnTimer


class ChatService:
    """Starting and continuing conversations (the command side)."""

    def __init__(
        self, conversations, sessions, flow, foundry, jobs, multilingual,
        agent_calls=None, trace=None, api_requests=None,
    ):
        self._conversations = conversations
        self._sessions = sessions
        self._flow = flow
        self._foundry = foundry
        self._jobs = jobs
        self._lang = multilingual
        # All three optional, so the existing tests can build a service without them.
        self._agent_calls = agent_calls
        self._trace = trace
        self._api_requests = api_requests

    def _timer(self, endpoint: str) -> TurnTimer:
        """Start measuring this request. Constructed here so the clock starts at the top
        of the service method, before any agent or DB work."""
        return TurnTimer(
            endpoint,
            agent_calls=self._agent_calls,
            api_requests=self._api_requests,
            trace=self._trace,
        )

    def start_chat(self, user_id: str, message: str) -> dict:
        """Start a new chat session and run the first turn.

        Creates a fresh Foundry conversation and a new session id, runs the flow for the
        opening message, translates the reply into the user's language, then persists the
        conversation, transcript and session rows.
        """
        # Started before the Foundry call, so its latency is counted too -- it is part of
        # what the user waits for.
        timer = self._timer("chat")
        failed = True
        session_id = "sess_" + uuid.uuid4().hex
        conversation = self._foundry.create_conversation()
        # Handed to flow.step, which fills in interaction_id / incident_id as it goes.
        # We keep our own reference so a failure can still report what was raised: the
        # dict is mutated in place, so incident_id is visible here even if step() throws
        # afterwards.
        flow_vars = {"user_id": user_id}
        try:
            messages, stage, flow_vars = self._flow.step(
                conversation.id, message, None, flow_vars
            )
            messages, answer, pending_job = self._persist_turn(
                user_id, conversation.id,
                question=message, messages=messages, stage=stage, flow_vars=flow_vars,
                session_id=session_id, seq=0,
            )
            self._sessions.save(user_id, session_id, conversation.id, message)
            self._dispatch_pending_job(user_id, conversation.id, pending_job)
            payload = chat_payload(
                user_id, session_id, conversation.id, stage, messages, answer
            )
            failed = False
            return payload
        except (NotFound, ConversationEnded):
            raise
        except Exception as exc:
            # Nothing is saved on a failed first turn, so the user must start over -- and
            # if a ticket was already raised, starting over would open a SECOND one.
            # Carry the incident out so the controller can hand them the number instead
            # of inviting a retry.
            raise TurnFailed(
                None, conversation.id, exc,
                incident_id=flow_vars.get("incident_id"),
            ) from exc
        finally:
            timer.finish(user_id, conversation.id, failed=failed)

    def continue_chat(self, user_id: str, session_id: str, message: str) -> dict:
        """Continue an ACTIVE conversation in a session by session_id.

        Loads the session's current conversation and advances it by one turn. If that
        conversation has already ended (stage=DONE) it is final -- we raise
        ConversationEnded so the frontend starts a fresh chat; there is no rollover and
        no memory carry-over.
        """
        # Before the two loads, so their DB time is included.
        timer = self._timer("chat_continue")
        failed = True
        try:
            session = self._sessions.load(user_id, session_id)
            if session is None:
                raise NotFound("Session not found")
            conv_id = session.get("current_conversation_id")
            conversation = self._conversations.load(user_id, conv_id)
            if conversation is None:
                raise NotFound("Conversation not found")
        except NotFound:
            # A 404 is still a served request, and its timing belongs in the table --
            # otherwise the latency chart only ever shows the happy path.
            timer.finish(user_id, None, failed=True)
            raise

        stage = conversation.get("stage", DONE)
        flow_vars = conversation.get("vars", {})

        # Ended conversations are final -- no rollover, no memory carry-over. The
        # frontend should start a new chat instead of continuing.
        if stage == DONE:
            # Not an error -- the caller turns it into a normal "chat ended" reply -- but
            # it IS a served request, so it is timed like any other.
            timer.finish(user_id, conv_id)
            raise ConversationEnded(
                "This conversation has ended. Please start a new chat."
            )

        # Anything that fails below leaves the conversation exactly where it was (the
        # writes are one transaction), so it is retryable -- and TurnFailed carries the
        # stage out, because the api layer cannot know it from the exception alone.
        # `current_stage` tracks what is genuinely in the DB: the old stage before the
        # write commits, the new one after.
        current_stage = stage
        try:
            messages, new_stage, new_flow_vars = self._flow.step(
                conv_id, message, stage, flow_vars
            )
            messages, answer, pending_job = self._persist_turn(
                user_id, conv_id,
                question=message, messages=messages, stage=new_stage,
                flow_vars=new_flow_vars,
            )
            current_stage = new_stage  # the transaction committed; the stage has moved
            self._dispatch_pending_job(user_id, conv_id, pending_job)
            failed = False
        except ConversationEnded:
            raise  # terminal, not retryable -- the caller shows the "ended" message
        except Exception as exc:
            raise TurnFailed(current_stage, conv_id, exc) from exc
        finally:
            timer.finish(user_id, conv_id, failed=failed)
        return chat_payload(user_id, session_id, conv_id, new_stage, messages, answer)

    # -- internals ----------------------------------------------------------
    def _persist_turn(
        self, user_id, conv_id, *, question, messages, stage, flow_vars,
        session_id=None, seq=None,
    ):
        """The tail every chat turn shares: translate, persist state, record transcript.

        Returns (messages, answer, pending_job). The caller dispatches `pending_job`
        once writing is done -- the conversation row must exist before a job_id can be
        recorded against it.
        """
        # If the flow asked to start an async job, pull the request out before saving so
        # it isn't persisted in vars; the caller dispatches it once the row exists.
        pending_job = flow_vars.pop("start_job", None)
        messages = visible(
            self._lang.translate_messages(messages, flow_vars.get("lang"))
        )
        answer = join(messages)
        # One transaction for both writes: the state and the transcript must agree, or a
        # crash between them leaves the stage advanced with the messages missing. Opened
        # here, AFTER the flow and translation calls, so no agent call is inside it.
        with self._conversations.transaction():
            self._conversations.save(
                user_id, conv_id,
                stage=stage, flow_vars=flow_vars, question=question, answer=answer,
                session_id=session_id, seq=seq,
            )
            # Record the FE-visible transcript for this turn: the user's message plus
            # each assistant line we're returning, exactly as shown.
            self._conversations.append_turns(
                user_id, conv_id,
                [("user", question)] + [("assistant", m) for m in messages],
            )
        return messages, answer, pending_job

    def _dispatch_pending_job(self, user_id, conv_id, pending):
        """Start an async job the flow requested (via flow_vars["start_job"]).

        No queue, no background loop: we call the agent's start() to get a job_id and
        record it on the conversation row (job_status=running). From then on the FE
        polls GET /jobs/status. Runs AFTER the conversation row is saved, so the row
        exists to update.

        `params` are the agent's own arguments, passed through by name -- the flow decided
        what this kind of job needs, and JobRunner decides which class provides it.
        """
        if not pending:
            return
        job_id = self._jobs.start(
            pending["kind"], conv_id, pending.get("params") or {}
        )
        # Capture the baseline at trigger time so a later poll can tell OUR fresh device
        # report apart from a stale one left by a previous run.
        self._conversations.start_job(
            user_id, conv_id, job_id, self._jobs.baseline_stamp()
        )
