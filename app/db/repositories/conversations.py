"""conversations + conversation_turns: flow state, job state and the transcript."""
import json
from typing import Optional

from app.core.constants import (
    DIAGNOSTICS_RUNNING,
    DONE,
    JOB_RUNNING,
    TROUBLESHOOT_RUNNING,
)
from app.db.database import fetchall_dicts, fetchone_dict
from app.db.repositories.base import BaseRepository, now_iso


def _vars_json(vars_val):
    """Serialize flow_vars for the `vars` column (already-serialized values pass through)."""
    return (
        json.dumps(vars_val, default=str)
        if isinstance(vars_val, (dict, list))
        else vars_val
    )


# Keys the flow keeps in flow_vars but which are stored in their OWN columns, so they can
# be indexed, joined and reported on instead of being buried in a JSON blob.
#
# The flow is unchanged by this: it still reads flow_vars.get("incident_id") in ~15
# places. Only the storage moved. save() lifts these out of flow_vars into the columns
# and strips them from the JSON (so there is one copy, not two), and load() merges them
# back in -- so what the flow receives looks exactly as it always did.
_PROMOTED = ("interaction_id", "incident_id", "resolved", "escalated")
_PROMOTED_FLAGS = ("resolved", "escalated")


def _split_vars(flow_vars):
    """-> (values for the promoted columns, JSON text for whatever is left).

    The flags are coerced to 0/1 rather than passed through: the columns are NOT NULL,
    and a mid-conversation turn has no "escalated" key at all. Without this a normal turn
    would try to write NULL and fail the constraint.
    """
    if not isinstance(flow_vars, dict):
        # Already-serialized vars (a re-save of a loaded row): nothing to lift out.
        return {k: None for k in _PROMOTED}, flow_vars
    promoted = {}
    for key in _PROMOTED:
        value = flow_vars.get(key)
        promoted[key] = (1 if value else 0) if key in _PROMOTED_FLAGS else value
    rest = {k: v for k, v in flow_vars.items() if k not in _PROMOTED}
    return promoted, _vars_json(rest)


def _merge_promoted(row):
    """Put the promoted columns back into row["vars"], in place.

    Keeps the flow's view of flow_vars identical to before the columns existed. The flags
    come back as real booleans, because 0/1 from SQLite and True/False from Azure SQL
    would otherwise behave differently in `if flow_vars.get("escalated")`.
    """
    merged = dict(row.get("vars") or {})
    for key in _PROMOTED:
        value = row.get(key)
        merged[key] = bool(value) if key in _PROMOTED_FLAGS else value
    row["vars"] = merged
    return row


class ConversationRepository(BaseRepository):
    """conversations + conversation_turns: flow state, job state and the transcript.

    Takes `use_sqlite` because find_running() needs a row limit, and that is the one
    thing the two dialects spell differently (LIMIT vs TOP).

    The job_* columns live on the conversation row (a job belongs to a conversation)
    rather than in a separate table: the FE reads job status by conversation_id, which
    this row already supports. The job methods touch ONLY the job_* columns via
    targeted UPDATEs, so they never clash with save() (which does not name them).
    """

    # THREE TIMESTAMPS, one job each:
    #   started_at        when the conversation began. Written once, never moves.
    #   last_updated_at   last activity of any kind -- every turn AND every job poll bump
    #                     it. The reaper's staleness filter and its ordering both depend
    #                     on that, which is why it is separate from the other two.
    #   ended_at          when the conversation reached DONE. Written once; NULL while
    #                     active, so "still open" is a plain IS NULL test.
    #
    # ended_at is stamped by the repository, not by the flow: the flow returns DONE from
    # TEN branches, plus an eleventh (job expiry) that lives in the service. One
    # write-once rule here cannot be forgotten by a branch added later.
    _COLS = (
        "id, user_id, conversation_id, stage, vars, question, answer, "
        "session_id, seq, title, started_at, last_updated_at, ended_at, "
        "interaction_id, incident_id, resolved, escalated"
    )
    _SQL_LOAD = (
        "SELECT " + _COLS + " FROM conversations WHERE user_id = ? AND id = ?"
    )
    # ended_at = COALESCE(ended_at, ?) is the write-once rule. The bound value is the
    # current time only when this save lands on DONE, otherwise NULL -- and
    # COALESCE(ended_at, NULL) leaves an already-set value alone. So the first save that
    # ends the conversation stamps it and nothing later can move it, with no extra read.
    _SQL_UPDATE = (
        "UPDATE conversations SET conversation_id = ?, stage = ?, vars = ?, "
        "question = ?, answer = ?, session_id = ?, seq = ?, "
        "title = ?, started_at = ?, last_updated_at = ?, "
        "ended_at = COALESCE(ended_at, ?), "
        "interaction_id = ?, incident_id = ?, resolved = ?, escalated = ? "
        "WHERE user_id = ? AND id = ?"
    )
    _SQL_INSERT = (
        "INSERT INTO conversations (id, user_id, conversation_id, stage, vars, "
        "question, answer, session_id, seq, title, started_at, last_updated_at, "
        "ended_at, interaction_id, incident_id, resolved, escalated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    _SQL_BY_SESSION = (
        "SELECT conversation_id, session_id, seq FROM conversations "
        "WHERE user_id = ? AND session_id = ? "
        "AND (stage IS NULL OR stage <> 'DONE') ORDER BY seq ASC"
    )
    _SQL_JOB = (
        "SELECT job_id, job_status, job_message, job_baseline, job_started_at "
        "FROM conversations WHERE user_id = ? AND id = ?"
    )
    # Rows the reaper must look at: a job still marked running, on a conversation still
    # parked in a RUNNING stage, that NOBODY HAS TOUCHED for a while.
    #
    # That last condition is what keeps the reaper a backstop instead of a second poller.
    # last_updated_at is bumped by every poll, so a browser polling every ~30s keeps its
    # own conversation out of this result set. Without it the reaper re-checked every
    # running job on every sweep, duplicating work the browser was already doing.
    #
    # ORDER BY last_updated_at, i.e. "whoever we have not looked at for the longest goes
    # first". This is what stops the batch limit starving anyone. Ordering by
    # job_started_at instead looked reasonable but never changed, so with more stuck jobs
    # than `limit` the same oldest-started rows were picked every sweep and the remainder
    # were never reached. Because a sweep bumps last_updated_at on everything it touches,
    # this ordering rotates by itself -- no extra state, and every job is eventually seen.
    _FIND_RUNNING_FROM = (
        "FROM conversations WHERE job_status = ? AND stage IN (?, ?) "
        "AND last_updated_at < ? ORDER BY last_updated_at ASC"
    )
    _SQL_FIND_RUNNING_SQLITE = (
        "SELECT user_id, id, job_started_at " + _FIND_RUNNING_FROM + " LIMIT ?"
    )
    _SQL_FIND_RUNNING_AZURE = (
        "SELECT TOP (?) user_id, id, job_started_at " + _FIND_RUNNING_FROM
    )
    _SQL_TURNS = (
        "SELECT role, content FROM conversation_turns "
        "WHERE user_id = ? AND conversation_id = ? ORDER BY seq ASC"
    )
    _SQL_MAX_SEQ = (
        "SELECT MAX(seq) FROM conversation_turns "
        "WHERE user_id = ? AND conversation_id = ?"
    )
    _SQL_INSERT_TURN = (
        "INSERT INTO conversation_turns "
        "(user_id, conversation_id, seq, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    # The compare-and-swap that makes job completion idempotent. Note the extra
    # `AND stage = ?` -- see advance_after_job().
    # ended_at gets the same write-once COALESCE as _SQL_UPDATE: a job finishing can end
    # the conversation outright (a failed diagnostic routes to the support team and goes
    # straight to DONE), and this UPDATE is the only write on that path.
    _SQL_ADVANCE_AFTER_JOB = (
        "UPDATE conversations SET stage = ?, vars = ?, question = ?, answer = ?, "
        "job_status = ?, job_message = ?, job_output = ?, "
        "last_updated_at = ?, ended_at = COALESCE(ended_at, ?), "
        "interaction_id = ?, incident_id = ?, resolved = ?, escalated = ? "
        "WHERE user_id = ? AND id = ? AND stage = ?"
    )
    # job_output is deliberately NOT selected here. get_job() feeds user-facing paths
    # (_already_advanced, and the "no active job" branch), and raw device output must
    # never reach the browser. Support reads it with load_job_output().
    _SQL_JOB_OUTPUT = (
        "SELECT job_output FROM conversations WHERE user_id = ? AND id = ?"
    )

    def __init__(self, conn, use_sqlite: bool = True):
        super().__init__(conn)
        self._use_sqlite = use_sqlite

    # -- flow state ---------------------------------------------------------
    def load(self, user_id: str, conv_id: str) -> Optional[dict]:
        """Load one conversation's saved state row, or None if it doesn't exist.

        Returns a dict keyed by column name, with `vars` already parsed from its JSON
        text back into a Python dict (empty dict when absent) AND the promoted columns
        merged into it -- so the flow sees the same flow_vars it always did, whether a
        value is stored in the JSON or in its own column.
        """
        cur = self._cursor()
        cur.execute(self._SQL_LOAD, (user_id, conv_id))
        row = fetchone_dict(cur)
        if row is None:
            return None
        row["vars"] = json.loads(row["vars"]) if row.get("vars") else {}
        return _merge_promoted(row)

    def save(
        self,
        user_id: str,
        conv_id: str,
        *,
        stage: str,
        flow_vars: dict,
        question: str,
        answer: str,
        session_id: Optional[str] = None,
        seq: Optional[int] = None,
    ) -> None:
        """Save a conversation turn: merge the given fields with any existing row.

        Lineage fields (session_id, seq) fall back to whatever is already stored when
        passed as None, so a normal turn need not re-supply them. On first save it
        stamps started_at and title (= the question), then upserts.

        ended_at is stamped here whenever `stage` is DONE -- including on a first save
        that ends immediately, which the non_outlook_it branch does (ticket raised, user
        thanked, conversation over in one turn).
        """
        now = now_iso()
        existing = self.load(user_id, conv_id) or {}
        item = {
            "id": conv_id,
            "user_id": user_id,
            "conversation_id": conv_id,
            "stage": stage,
            "vars": flow_vars,
            "question": question,
            "answer": answer,
            "last_updated_at": now,
            # lineage: use the passed value, else keep what's already stored
            "session_id": (
                session_id if session_id is not None else existing.get("session_id")
            ),
            "seq": seq if seq is not None else existing.get("seq", 0),
            "title": question if not existing else existing.get("title"),
            "started_at": now if not existing else existing.get("started_at", now),
            # Only non-NULL on the save that ends the conversation; the COALESCE in
            # _SQL_UPDATE then makes it write-once.
            "ended_at": now if stage == DONE else None,
        }
        self._upsert(item)

    def _upsert(self, item: dict) -> None:
        """Insert or update a conversation row.

        Lifts the promoted keys (the ServiceNow ids and the two outcome flags) out of
        flow_vars into their own columns and serializes only what is LEFT into `vars`, so
        each value is stored once rather than in two places that can disagree. Then tries
        an UPDATE by primary key (user_id + id); if it matched no row (rowcount == 0),
        INSERTs instead. One code path that works on both SQLite and Azure SQL.
        """
        promoted, vars_json = _split_vars(item.get("vars"))
        cur = self._cursor()
        cur.execute(
            self._SQL_UPDATE,
            (
                item.get("conversation_id"), item.get("stage"), vars_json,
                item.get("question"), item.get("answer"), item.get("session_id"),
                item.get("seq"), item.get("title"), item.get("started_at"),
                item.get("last_updated_at"), item.get("ended_at"),
                promoted["interaction_id"], promoted["incident_id"],
                promoted["resolved"], promoted["escalated"],
                item.get("user_id"), item.get("id"),
            ),
        )
        if cur.rowcount == 0:
            cur.execute(
                self._SQL_INSERT,
                (
                    item.get("id"), item.get("user_id"), item.get("conversation_id"),
                    item.get("stage"), vars_json, item.get("question"),
                    item.get("answer"), item.get("session_id"), item.get("seq"),
                    item.get("title"), item.get("started_at"),
                    item.get("last_updated_at"), item.get("ended_at"),
                    promoted["interaction_id"], promoted["incident_id"],
                    promoted["resolved"], promoted["escalated"],
                ),
            )
        self._commit()

    def list_by_session(self, user_id: str, session_id: str) -> list:
        """Return the session's still-active conversations, oldest first."""
        cur = self._cursor()
        cur.execute(self._SQL_BY_SESSION, (user_id, session_id))
        return fetchall_dicts(cur)

    # -- transcript ---------------------------------------------------------
    def append_turns(self, user_id: str, conv_id: str, turns) -> None:
        """Append FE-visible messages to conversation_turns (Approach A).

        `turns` is a list of (role, content) pairs in display order -- typically the
        user's message followed by each assistant line we return that turn. This is the
        EXACT text shown to the user (post-translation, no agent JSON), so the
        conversations API can replay it verbatim. Blank/whitespace-only contents are
        skipped. Each row gets the next sequential seq for this conversation, so history
        always reads back in the order the user saw it.
        """
        rows = [(role, content) for role, content in turns if content and content.strip()]
        if not rows:
            return
        now = now_iso()
        cur = self._cursor()
        # Next seq = one past the current max for this conversation (0 on first write).
        cur.execute(self._SQL_MAX_SEQ, (user_id, conv_id))
        row = cur.fetchone()
        next_seq = (row[0] + 1) if row and row[0] is not None else 0
        for role, content in rows:
            cur.execute(
                self._SQL_INSERT_TURN,
                (user_id, conv_id, next_seq, role, content, now),
            )
            next_seq += 1
        self._commit()

    def load_turns(self, user_id: str, conv_id: str) -> list:
        """Return a conversation's FE-visible transcript as [{role, content}], oldest
        first. Empty list when nothing was recorded (e.g. chats created before Approach
        A)."""
        cur = self._cursor()
        cur.execute(self._SQL_TURNS, (user_id, conv_id))
        return [{"role": r[0], "content": r[1]} for r in cur.fetchall()]

    # -- job state (the job_* columns on the conversation row) --------------
    def start_job(
        self, user_id: str, conv_id: str, job_id: str, baseline: str = None
    ) -> None:
        """Mark the conversation's job as just-started (running).

        `baseline` is the trigger-time timestamp (see JobRunner.baseline_stamp);
        check_status uses it to tell OUR fresh result from a stale run-state row.
        """
        now = now_iso()
        self._cursor().execute(
            "UPDATE conversations SET job_id = ?, job_status = ?, job_message = ?, "
            "job_output = NULL, job_baseline = ?, job_started_at = ?, "
            "last_updated_at = ? WHERE user_id = ? AND id = ?",
            (job_id, JOB_RUNNING, "Starting...", baseline, now, now, user_id, conv_id),
        )
        self._commit()

    def find_running(self, limit: int, stale_before: str) -> list:
        """Running jobs that nobody has touched since `stale_before` -- the reaper's list.

        `stale_before` is an ISO-8601 UTC timestamp; anything updated more recently is
        assumed to have a browser watching it and is skipped. This only FINDS them; the
        caller decides what to do with each.
        """
        cur = self._cursor()
        # TOP (?) comes BEFORE the WHERE values in T-SQL; LIMIT ? comes after them.
        where = (JOB_RUNNING, DIAGNOSTICS_RUNNING, TROUBLESHOOT_RUNNING, stale_before)
        if self._use_sqlite:
            cur.execute(self._SQL_FIND_RUNNING_SQLITE, where + (limit,))
        else:
            cur.execute(self._SQL_FIND_RUNNING_AZURE, (limit,) + where)
        return fetchall_dicts(cur)

    def update_job_message(self, user_id: str, conv_id: str, message: str) -> None:
        """Record the progress line for one still-running poll. ONE column, ONE statement.

        This used to be two calls and two commits -- update_job_message + save() -- because
        the poll COUNTER lived inside vars. Expiry is now measured from job_started_at in
        wall-clock time, so there is no counter, nothing to write into vars, and nothing
        that can be left half-updated.

        It also does not touch question/answer. save() used to stamp question="[job-poll]"
        over the user's real last message on every poll. Nothing reads those columns -- the
        transcript lives in conversation_turns -- so it was pure noise.
        """
        self._cursor().execute(
            "UPDATE conversations SET job_status = ?, job_message = ?, "
            "last_updated_at = ? WHERE user_id = ? AND id = ?",
            (JOB_RUNNING, message, now_iso(), user_id, conv_id),
        )
        self._commit()

    def advance_after_job(
        self,
        user_id: str,
        conv_id: str,
        *,
        expected_stage: str,
        new_stage: str,
        flow_vars: dict,
        question: str,
        answer: str,
        job_status: str,
        job_message: str,
        job_output=None,
    ) -> bool:
        """Move the conversation off a RUNNING stage and record the job outcome.

        This is ONE conditional UPDATE, and that is the whole point. It replaces the
        old finish_job/fail_job + save pair, which wrote the job outcome and the new
        stage separately -- so a crash between them left job_status='done' with the
        stage never advanced.

        The `AND stage = ?` in the WHERE clause is a compare-and-swap: only the FIRST
        caller still sees the RUNNING stage and updates 1 row. A second poller -- two
        browser tabs, a refresh mid-poll, another worker process, or the reaper running
        on a second instance -- updates 0 rows and must NOT advance the flow or write the
        transcript again.

        Returns True if this caller won the race.

        Call it inside transaction() together with append_turns, so the stage move and
        the transcript rows commit as one unit. A concurrent caller then blocks on the
        row lock and correctly sees 0 rows once the winner commits.
        """
        now = now_iso()
        # Same split as _upsert: the ServiceNow ids and the outcome flags go to their own
        # columns, and only what is left is serialized into `vars`. A job finishing can
        # end the conversation outright (a failed diagnostic routes to the support team),
        # so escalated/resolved must be written on this path too.
        promoted, vars_json = _split_vars(flow_vars)
        cur = self._cursor()
        cur.execute(
            self._SQL_ADVANCE_AFTER_JOB,
            (
                new_stage, vars_json, question, answer,
                job_status, job_message, job_output, now,
                now if new_stage == DONE else None,
                promoted["interaction_id"], promoted["incident_id"],
                promoted["resolved"], promoted["escalated"],
                user_id, conv_id, expected_stage,
            ),
        )
        won = cur.rowcount == 1
        self._commit()
        return won

    def load_job_output(self, user_id: str, conv_id: str):
        """The raw device script output, for SUPPORT use only.

        Deliberately a separate method from get_job(): nothing on a user-facing path
        should be able to read this column by accident. If you call this, the value must
        not end up in an HTTP response.
        """
        cur = self._cursor()
        cur.execute(self._SQL_JOB_OUTPUT, (user_id, conv_id))
        row = cur.fetchone()
        return row[0] if row else None

    def get_job(self, user_id: str, conv_id: str) -> dict:
        """Return {job_id, job_status, job_message, job_baseline}.

        job_output is deliberately excluded -- see _SQL_JOB.
        """
        cur = self._cursor()
        cur.execute(self._SQL_JOB, (user_id, conv_id))
        return fetchone_dict(cur) or {}
