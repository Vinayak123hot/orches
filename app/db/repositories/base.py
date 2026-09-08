"""Shared connection, commit and transaction handling for the repositories."""
from contextlib import contextmanager
from datetime import datetime, timezone


def now_iso() -> str:
    """Current time as an ISO-8601 UTC string (how every timestamp column is stored).

    Public (no underscore) because every repository module uses it: timestamps are
    written by the repository rather than the caller, so that "when did this happen?"
    means one clock and one format across every table.
    """
    return datetime.now(timezone.utc).isoformat()


class BaseRepository:
    """Shared connection and commit handling for the repositories."""

    def __init__(self, conn):
        self._conn = conn
        self._in_transaction = False

    def _cursor(self):
        return self._conn.cursor()

    def _commit(self) -> None:
        """Commit this write -- unless we are inside a transaction() block.

        Every write method calls this instead of conn.commit() directly, so a single
        write still commits on its own (as before), but a group of writes wrapped in
        transaction() commits exactly once, at the end.
        """
        if not self._in_transaction:
            self._conn.commit()

    @contextmanager
    def transaction(self):
        """Group several writes into ONE commit -- all of them, or none.

        Used by the service layer around the write sequences that must agree with each
        other (completing a job; persisting a chat turn). Without it, each write
        committed separately, so a crash part-way through left the conversation row
        advanced but its transcript rows missing.

        Open this as LATE as possible and keep it short: an open transaction holds
        locks on the row, so it must never wrap an agent call.
        """
        if self._in_transaction:
            yield self  # already inside one -- the outermost block owns the commit
            return
        self._in_transaction = True
        try:
            yield self
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            self._in_transaction = False
