"""sessions: the chat sessions shown in the frontend's left panel."""
from typing import Optional

from app.db.database import fetchall_dicts, fetchone_dict
from app.db.repositories.base import BaseRepository, now_iso


class SessionRepository(BaseRepository):
    """sessions: the chat sessions shown in the frontend's left panel."""

    # Same two names as conversations, for the same reasons:
    #   started_at       when the session was first created. Written once, never moves.
    #   last_updated_at  bumped every time the session is pointed at a new turn. The
    #                    sidebar is ordered by it, so it is what puts the chat a user
    #                    just spoke in at the top of their list.
    #
    # There is deliberately NO ended_at here. A session has no terminal state of its own
    # -- it drops off the sidebar when its CURRENT conversation reaches DONE, which is
    # what the LEFT JOIN below tests. Adding one would need a second rule about what
    # "ending" a session even means.
    _COLS = (
        "id, session_id, user_id, current_conversation_id, title, "
        "started_at, last_updated_at"
    )
    _SQL_LOAD = "SELECT " + _COLS + " FROM sessions WHERE user_id = ? AND id = ?"
    _SQL_UPDATE = (
        "UPDATE sessions SET session_id = ?, current_conversation_id = ?, "
        "title = ?, started_at = ?, last_updated_at = ? WHERE user_id = ? AND id = ?"
    )
    _SQL_INSERT = (
        "INSERT INTO sessions (id, session_id, user_id, current_conversation_id, "
        "title, started_at, last_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    # Sidebar list. Excludes sessions whose current conversation has ended
    # (stage = DONE), so ended chats drop off the left panel. The row limit is spelled
    # differently per dialect (LIMIT vs TOP), hence the two statements.
    _LIST_COLS = (
        "s.id, s.session_id, s.current_conversation_id, s.title, "
        "s.started_at, s.last_updated_at"
    )
    _LIST_FROM = (
        "FROM sessions s "
        "LEFT JOIN conversations c "
        "  ON c.user_id = s.user_id AND c.id = s.current_conversation_id "
        "WHERE s.user_id = ? AND (c.stage IS NULL OR c.stage <> 'DONE') "
    )
    _SQL_LIST_SQLITE = (
        "SELECT " + _LIST_COLS + " " + _LIST_FROM
        + "ORDER BY s.last_updated_at DESC LIMIT 20"
    )
    _SQL_LIST_AZURE = (
        "SELECT TOP 20 " + _LIST_COLS + " " + _LIST_FROM
        + "ORDER BY s.last_updated_at DESC"
    )

    def __init__(self, conn, use_sqlite: bool):
        super().__init__(conn)
        self._use_sqlite = use_sqlite

    def load(self, user_id: str, session_id: str) -> Optional[dict]:
        """Load one session row (by user_id + session_id) as a dict, or None."""
        cur = self._cursor()
        cur.execute(self._SQL_LOAD, (user_id, session_id))
        return fetchone_dict(cur)

    def save(
        self, user_id: str, session_id: str, current_conversation_id: str, title: str
    ) -> None:
        """Save/refresh a session: point it at the current conversation and bump
        last_updated_at, preserving the original started_at and title."""
        now = now_iso()
        existing = self.load(user_id, session_id) or {}
        self._upsert(
            {
                "id": session_id,
                "session_id": session_id,
                "user_id": user_id,
                "current_conversation_id": current_conversation_id,
                "last_updated_at": now,
                "started_at": existing.get("started_at", now),
                "title": existing.get("title", title),
            }
        )

    def _upsert(self, item: dict) -> None:
        """Insert or update a session row -- same update-then-insert as the
        conversation upsert, but for the sessions table."""
        cur = self._cursor()
        cur.execute(
            self._SQL_UPDATE,
            (
                item.get("session_id"), item.get("current_conversation_id"),
                item.get("title"), item.get("started_at"),
                item.get("last_updated_at"),
                item.get("user_id"), item.get("id"),
            ),
        )
        if cur.rowcount == 0:
            cur.execute(
                self._SQL_INSERT,
                (
                    item.get("id"), item.get("session_id"), item.get("user_id"),
                    item.get("current_conversation_id"), item.get("title"),
                    item.get("started_at"), item.get("last_updated_at"),
                ),
            )
        self._commit()

    def list_recent(self, user_id: str) -> list:
        """A user's most recent still-active sessions (up to 20), newest first."""
        cur = self._cursor()
        cur.execute(
            self._SQL_LIST_SQLITE if self._use_sqlite else self._SQL_LIST_AZURE,
            (user_id,),
        )
        return fetchall_dicts(cur)
