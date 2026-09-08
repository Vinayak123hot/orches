"""The job reaper: finish off device jobs nobody is watching.

WHY THIS EXISTS
A job only ever moves forward when the FRONTEND polls GET /jobs/status. So if the user
closed their tab -- or slept the laptop, or lost signal -- the conversation stayed in
DIAGNOSTICS_RUNNING permanently: the stage never advanced, the ServiceNow ticket kept
saying "Automated diagnostics started" and nothing more, and it sat in their sidebar as
an active chat that did nothing. The old give-up rule couldn't help, because it counted
POLLS, and a poll only happens while a browser is open.

WHAT IT DOES
Every REAPER_INTERVAL_SECONDS it asks for conversations still parked in a RUNNING stage
and puts each one through JobService.advance_job() -- the exact method the FE poll calls.
So a sweep and a poll are indistinguishable to the rest of the system: same status check,
same compare-and-swap, same transaction, same tests.

Mostly it finds nothing. If the user is watching, their own poll advances the job first
and the sweep is a no-op (their conversation is filtered out by REAPER_STALE_MINUTES).
It only matters for the conversations that were abandoned.

WHY A BACKGROUND TASK, AND WHAT THAT COSTS
This was an Azure Functions timer trigger. On App Service there is no platform timer, so
it is an asyncio task started by the app's lifespan (see main.py). Two consequences:

  * IT NEEDS "ALWAYS ON". A task inside the app cannot wake an app that has idled out,
    which the platform timer could. Without Always On the sweeps simply stop, and
    abandoned jobs are never finished -- the bug this module exists to prevent.
  * IT RUNS ON EVERY INSTANCE (and in every gunicorn worker), where the timer trigger
    took a lease and ran once. That is SAFE rather than merely tolerable: the sweep only
    selects jobs nobody has touched for REAPER_STALE_MINUTES, and the conditional UPDATE
    in advance_after_job() is a compare-and-swap, so exactly one sweeper can complete any
    given job. The cost is duplicated READS -- raise the interval, not the batch size, if
    that ever shows up in DTU.

The sweep itself is blocking (SQL plus agent calls), so it runs in a worker thread via
asyncio.to_thread. Running it directly on the event loop would stall every request being
served by this worker for the length of the sweep.

It does NOT notify anyone. It makes the state correct; telling the user their diagnostic
finished while they were away is a separate feature (email / Teams / SignalR).
"""
import asyncio
import random

from app.core.config import logger
from app.deps import database, make_job_service

# Cap on the random start delay. Without it, every instance that starts after a deploy
# sweeps at the same moment for the life of the process; with it they spread out and stay
# spread out.
MAX_START_JITTER_SECONDS = 60


def sweep_once() -> dict:
    """One sweep, on its own connection. Blocking -- call it in a thread.

    Its own connection because there is no request here and so no Depends-managed one,
    and it is closed in a `finally` for the same reason: a leaked connection per sweep
    would exhaust the pool in a day.
    """
    conn = database.connect()
    try:
        result = make_job_service(conn).sweep_running_jobs()
        # Logged at info even when empty: a silent reaper and a dead reaper look the same
        # otherwise, and this is the line that proves it is alive.
        logger.info(
            "reaper sweep: found=%s advanced=%s still_running=%s failed=%s",
            result["found"], result["advanced"],
            result["still_running"], result["failed"],
        )
        return result
    finally:
        conn.close()


async def run_forever(interval_seconds: int) -> None:
    """Sweep every `interval_seconds` until cancelled.

    Never lets an exception end the loop: a sweep that fails must not stop every LATER
    sweep, which is what makes this a backstop rather than a single attempt. CancelledError
    is re-raised untouched so shutdown is immediate (it derives from BaseException, so the
    `except Exception` below would not catch it anyway -- it is caught explicitly to say
    that this is intended).
    """
    # Sleep BEFORE the first sweep: at startup every running job was just being polled by
    # somebody, and a burst of DB work while the app is still warming helps nobody.
    await asyncio.sleep(random.uniform(0, min(interval_seconds, MAX_START_JITTER_SECONDS)))
    while True:
        try:
            await asyncio.to_thread(sweep_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reaper sweep failed")
        await asyncio.sleep(interval_seconds)
