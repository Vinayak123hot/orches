"""api_requests: one row per HTTP request we serve."""
from app.core.config import logger
from app.db.database import fetchall_dicts
from app.db.repositories.base import BaseRepository, now_iso


class ApiRequestRepository(BaseRepository):
    """api_requests: one row per HTTP request we serve.

    Same rules as AgentCallRepository -- insert-only, its own commit, never raises -- and
    for the same reasons. See that class for the ordering rule; this one is written from
    the same finally block.

    The one difference is that a row is written even when NOTHING else happened: a status
    poll that returns early, or a request that failed before reaching an agent, still
    gets its timing recorded. That is the whole point of the table. Building latency
    charts from conversation_turns or agent_calls silently omits those, and they are the
    slow and broken requests.
    """

    _SQL_INSERT = (
        "INSERT INTO api_requests (user_id, conversation_id, endpoint, duration_ms, "
        "agent_ms, http_status, is_error, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    _COLS = (
        "id, conversation_id, endpoint, duration_ms, agent_ms, http_status, "
        "is_error, started_at"
    )
    _SQL_BY_CONVERSATION = (
        "SELECT " + _COLS + " FROM api_requests "
        "WHERE user_id = ? AND conversation_id = ? ORDER BY id ASC"
    )

    def add(
        self, user_id: str, conv_id, endpoint: str, duration_ms: int,
        agent_ms=None, http_status=None, is_error: bool = False,
        started_at=None,
    ) -> None:
        """Record one served request. Never raises.

        Swallows its own failures for the same reason append_calls does: a timing table
        must not be able to fail the request it is timing.
        """
        try:
            self._cursor().execute(
                self._SQL_INSERT,
                (
                    user_id,
                    conv_id,
                    endpoint,
                    int(duration_ms or 0),
                    None if agent_ms is None else int(agent_ms),
                    http_status,
                    1 if is_error else 0,
                    started_at or now_iso(),
                ),
            )
            self._conn.commit()
        except Exception:
            logger.exception(
                "could not write api_requests row conv_id=%s endpoint=%s (dropped)",
                conv_id, endpoint,
            )

    def list_for_conversation(self, user_id: str, conv_id: str) -> list:
        """Every request served for one conversation, oldest first."""
        cur = self._cursor()
        cur.execute(self._SQL_BY_CONVERSATION, (user_id, conv_id))
        return fetchall_dicts(cur)
