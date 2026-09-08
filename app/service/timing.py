"""TurnTimer: measures one served request and writes both timing tables."""
import time
from datetime import datetime, timezone


class TurnTimer:
    """Measures one served request and writes both timing tables when it finishes.

    Held per request (deps.py builds it alongside the trace), because it carries this
    request's start time.

    WHERE IT IS WRITTEN -- from a finally, AFTER the transaction() block.

    Both repositories share this request's connection, so their commit is that
    connection's commit. Called inside an open transaction() they would commit the turn's
    half-written work early and defeat the all-or-nothing boundary. Once the block has
    exited there is nothing in flight for the commit to catch.

    And in a `finally` so it runs on the FAILURE path too -- which is when both tables
    matter most. The agent_calls row holds the error of the call that broke the turn, and
    the api_requests row is the slow/failed request a latency chart most needs. Tie
    either to the rolled-back transaction and that evidence is discarded with it.

    WHAT duration_ms MEANS -- it is measured from the start of the service method, not
    from the edge. So it covers the agent calls, the flow, translation and the DB writes,
    but NOT FastAPI's request parsing and response serialization. Those are single-digit
    milliseconds against turns of 1-4 seconds. True edge-to-edge would need middleware,
    and middleware runs outside the Depends lifecycle -- the DB connection is already
    closed by then, so it would need its own, doubling connection demand per request.
    """

    def __init__(self, endpoint: str, agent_calls=None, api_requests=None, trace=None):
        self._endpoint = endpoint
        self._agent_calls = agent_calls
        self._api_requests = api_requests
        self._trace = trace
        self._t0 = time.perf_counter()
        # Wall clock for storage; perf_counter (monotonic) for the duration, so an NTP
        # adjustment mid-request cannot produce a negative number.
        self._started_at = datetime.now(timezone.utc).isoformat()

    def finish(self, user_id: str, conv_id=None, *, failed: bool = False) -> None:
        """Write the agent_calls rows and the api_requests row. Never raises."""
        rows = list(self._trace.rows) if self._trace else []
        # Summed from the rows we already hold -- no extra read. An untraced call simply
        # is not counted, which is the honest answer: we cannot report time we did not
        # measure.
        agent_ms = sum(int(r.get("duration_ms") or 0) for r in rows) if rows else None

        if self._agent_calls is not None and rows:
            self._agent_calls.append_calls(user_id, conv_id, rows)
            self._trace.rows.clear()  # a retry on the same object cannot double-write

        if self._api_requests is not None:
            # Written even with no agent rows at all: a status poll that returned early,
            # or a request that failed before reaching an agent, still gets its timing.
            self._api_requests.add(
                user_id, conv_id, self._endpoint,
                int((time.perf_counter() - self._t0) * 1000),
                agent_ms=agent_ms,
                is_error=failed,
                started_at=self._started_at,
            )
