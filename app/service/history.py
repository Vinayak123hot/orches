"""HistoryService: the read-only queries."""


class HistoryService:
    """Read-only queries -- these ARE served straight from the DB.

    No timer and no trace: these calls touch no agent, so there is nothing to time
    against an agent budget, and adding rows for them would dilute the tables that exist
    to explain slow turns.
    """

    def __init__(self, conversations, sessions):
        self._conversations = conversations
        self._sessions = sessions

    def list_sessions(self, user_id: str) -> dict:
        """Sidebar data: a user's most recent sessions, newest first.

        Messages are not included -- the frontend fetches those per session via
        get_transcript().
        """
        return {"sessions": self._sessions.list_recent(user_id)}

    def get_transcript(self, user_id: str, session_id: str) -> dict:
        """Return the session's messages as a flat list of {role, content}.

        `conversations` is every turn we showed the user (from conversation_turns,
        Approach A), concatenated in order -- no other conversation metadata.
        """
        conversations = []
        for row in self._conversations.list_by_session(user_id, session_id):
            conversations.extend(
                self._conversations.load_turns(user_id, row["conversation_id"])
            )
        return {
            "user_id": user_id,
            "session_id": session_id,
            "conversations": conversations,
        }
