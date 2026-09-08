"""agent_calls: the internal trace of every agent call."""
from app.core.config import logger
from app.db.database import fetchall_dicts
from app.db.repositories.base import BaseRepository, now_iso


class AgentCallRepository(BaseRepository):
    """agent_calls: the internal trace of every agent call.

    Insert-only, like append_turns -- but with two deliberate differences.

    No seq read. append_turns has to run MAX(seq)+1 first because the FE replays turns
    in display order. Here the database assigns the id, so it is one statement instead
    of two and two concurrent pollers cannot pick the same number.

    Its own commit, NOT the caller's transaction. Every other write in this package
    defers to transaction() so a turn commits once. This one must not: if the turn rolls
    back, the trace of the call that CAUSED the rollback would be rolled back with it,
    which destroys exactly the evidence the table exists to keep.

    ORDERING RULE -- append_calls() must be called AFTER the turn's transaction() block
    has exited, never inside it.

    This repository shares the request's single connection, so its commit() is that
    connection's commit(). Called inside an open transaction() it would commit the
    caller's half-written work early -- silently defeating the all-or-nothing boundary
    that transaction() exists to provide. (Verified: a rollback after an inner
    append_calls() left the conversation row committed.)

    Called after the block, both cases are correct: the turn has already committed or
    already rolled back, so there is no in-flight work for this commit to catch. A
    `finally` around the turn is the right place -- it runs on the failure path too,
    which is when the trace matters most. TurnTimer does exactly that.
    """

    _SQL_INSERT = (
        "INSERT INTO agent_calls (user_id, conversation_id, agent, request_text, "
        "response_json, duration_ms, is_error, error_text, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    _COLS = (
        "id, agent, request_text, response_json, duration_ms, is_error, "
        "error_text, created_at"
    )
    _SQL_BY_CONVERSATION = (
        "SELECT " + _COLS + " FROM agent_calls "
        "WHERE user_id = ? AND conversation_id = ? ORDER BY id ASC"
    )

    def append_calls(self, user_id: str, conv_id: str, calls) -> None:
        """Write one row per agent call. Never raises.

        `calls` is a list of dicts, each {agent, request_text, response_json,
        duration_ms, is_error, error_text} -- collected during the turn and flushed
        here in one go, so four agent calls cost one round trip rather than four.

        Swallows its own failures on purpose. This is a logging table: if the insert
        fails, the turn the user is waiting on must still succeed. A tracing table that
        can take the product down is a bad trade, so the exception is logged and
        dropped.
        """
        if not calls:
            return
        now = now_iso()
        try:
            cur = self._cursor()
            for call in calls:
                cur.execute(
                    self._SQL_INSERT,
                    (
                        user_id,
                        conv_id,
                        call.get("agent") or "unknown",
                        call.get("request_text"),
                        call.get("response_json"),
                        int(call.get("duration_ms") or 0),
                        1 if call.get("is_error") else 0,
                        call.get("error_text"),
                        now,
                    ),
                )
            self._conn.commit()
        except Exception:
            logger.exception(
                "could not write agent_calls trace conv_id=%s (%d row(s) dropped)",
                conv_id, len(calls),
            )

    def list_for_conversation(self, user_id: str, conv_id: str) -> list:
        """The full trace for one conversation, oldest first -- for support/debugging.

        NOT for a user-facing endpoint: response_json holds raw agent output, which for
        the device agents carries profile paths, mailbox addresses and internal host
        names.
        """
        cur = self._cursor()
        cur.execute(self._SQL_BY_CONVERSATION, (user_id, conv_id))
        return fetchall_dicts(cur)
