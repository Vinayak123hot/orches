"""Settings (environment / App Settings) and the shared logger.

Every environment-derived value lives on the Settings class below, read once into the
module-level `settings` singleton. Domain constants that are NOT configuration (stage
names, agent vocabularies, fixed prompts) live in constants.py.

Why a class instead of ~20 module-level os.getenv() calls:
  * a test can build Settings({"JOBS_DUMMY": "false", ...}) instead of reimporting
    the module to change one value;
  * parsing and defaults live in one place, so "was this an int or a string?" has one
    answer;
  * warn_on_risky_config() gives us a single startup check -- which is what catches a
    misconfiguration (e.g. JOBS_DUMMY left on in production) instead of it silently
    defaulting and telling real users their machine was repaired.

No secrets or URLs are hardcoded. Locally the values come from a .env file (loaded
below); on App Service they are Application Settings, which arrive as plain environment
variables -- so the same code reads both. See .env.example for the full list.
"""
import logging
import os
from typing import Mapping, Optional

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Application bootstrap: environment variables + logging
# ---------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("orchestrator_api")


def _csv(raw: str) -> list:
    """Split a comma-separated App Setting into a clean list."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _flag(raw: str) -> bool:
    """Parse a boolean App Setting (they arrive as strings)."""
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(raw: str, default: int) -> int:
    """Parse an int App Setting, falling back rather than crashing on junk."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("expected an integer setting, got %r; using %s", raw, default)
        return default


class Settings:
    """All environment-derived configuration, parsed once.

    `env` defaults to os.environ; pass a plain dict in tests to build a Settings with
    whatever values that test needs.
    """

    def __init__(self, env: Optional[Mapping[str, str]] = None):
        get = (env if env is not None else os.environ).get

        # -- Frontend / transport -------------------------------------------
        self.CORS_ORIGINS = _csv(
            get("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
        )
        # Every route is served under this prefix, so the endpoints are:
        #     POST /api/chat        POST /api/chat/continue
        #     GET  /api/jobs/status
        #     GET  /api/sessions/{user_id}
        #     GET  /api/conversations/{user_id}/{session_id}
        #
        # A setting rather than a constant so the prefix can move with configuration
        # instead of code -- it was /workflow under the Functions host, which mounted the
        # whole app there. The frontend's VITE_API_URL must include it.
        #
        # The health endpoint is deliberately NOT under it (see main.py), so a platform
        # probe keeps working if the prefix ever changes again.
        self.API_PREFIX = get("API_PREFIX", "/api")
        # Seconds an agent gets for ONE call, passed to every agent in agents/ so the
        # per-turn timeout budget is set in one place rather than seven.
        #
        # There are no *_AGENT_URL settings any more. The agents are not Function Apps we
        # POST to -- they are classes in agents/<name>/, constructed in deps.py and called
        # directly (see agents/__init__.py). Whatever an agent needs to reach its own
        # backend (a Foundry agent name, a Graph permission, a KB index) is that agent's
        # configuration and is read in its own folder, which is why nothing here names an
        # individual agent.
        self.AGENT_HTTP_TIMEOUT = _int(get("AGENT_HTTP_TIMEOUT", "120"), 120)

        # -- Language policy -------------------------------------------------
        # The language the flow works in internally: the inbound message is translated
        # into it before the yes/no checks and the other agents run, and outgoing
        # messages are translated back out of it. Also the language assumed when
        # detection is unavailable, uncertain or unsupported -- and outgoing messages are
        # NOT translated when the detected language equals this one.
        self.DEFAULT_LANG = get("DEFAULT_LANG", "en")

        # -- Flow policy ----------------------------------------------------
        self.MAX_NONACTIONABLE = _int(get("MAX_NONACTIONABLE", "2"), 2)

        # TEMPORARY -- the Intune device every job is targeted at.
        #
        # The conversation carries user_id but nothing about a machine, so the
        # orchestrator genuinely cannot know which device to remediate. Until that is
        # settled (either the frontend supplies it, or the diagnostics service resolves
        # user -> device via Graph), every diagnostic and troubleshoot job runs against
        # this one test device. warn_on_risky_config() says so loudly at startup, because
        # remediating the wrong machine is the worst failure available here.
        self.DEFAULT_DEVICE_ID = get(
            "DEFAULT_DEVICE_ID", "d57cc3db-d2ad-42c3-a855-476359ac0aac"
        )

        # -- State store ----------------------------------------------------
        # If SQLITE_DB_PATH is set, use a LOCAL SQLite file instead of Azure SQL.
        # Handy for testing on a machine that can't reach Azure SQL. Leave it empty
        # to use the real Azure SQL connection (SQL_CONNECTION_STRING).
        self.SQLITE_DB_PATH = get("SQLITE_DB_PATH", "")
        self.SQL_CONNECTION_STRING = get("SQL_CONNECTION_STRING", "")

        # -- Azure SQL connection pool ---------------------------------------
        # mssql-python pools by default (max_size=100, idle_timeout=600). We set
        # it explicitly so the intended ceiling is visible in the code, and so
        # raising the threadpool later can't silently open hundreds of sessions.
        #
        # SIZING: a request holds exactly ONE connection (get_db is cached per
        # request, so both repositories share it), and only a threadpool worker
        # can hold one -- so the worker count (40 by default) covers the request
        # path entirely. The few spare are for connections taken OUTSIDE the
        # threadpool: the reaper's background sweep, and any health check that
        # queries SQL (/health deliberately does not -- see main.py).
        #
        # ON APP SERVICE, MULTIPLY BY THE WORKER COUNT. gunicorn runs several
        # processes per instance, so the real total is
        #     max_size x gunicorn workers x instances
        # -- see the note in startup.sh before raising either number.
        #
        # The pool MUST be >= the worker count: mssql-python raises immediately
        # when the pool is exhausted, it does not queue and wait (there is no
        # pool_timeout equivalent). A pool smaller than the worker count turns a
        # brief shortage into failed requests.
        #
        # TOTAL sessions against Azure SQL = max_size x processes x instances.
        # Check the tier's max concurrent sessions before raising either number.
        self.SQL_POOL_MAX_SIZE = _int(get("SQL_POOL_MAX_SIZE", "45"), 45)
        # Seconds a connection may sit UNUSED before it is closed and dropped
        # from the pool -- housekeeping, so we don't hold connections open all
        # night. This is NOT a "wait for a free connection" timeout.
        self.SQL_POOL_IDLE_TIMEOUT = _int(get("SQL_POOL_IDLE_TIMEOUT", "300"), 300)

        # -- Azure AI Foundry ------------------------------------------------
        self.AZURE_FOUNDRY_PROJECT_ENDPOINT = get(
            "AZURE_FOUNDRY_PROJECT_ENDPOINT", ""
        )
        # Seconds allowed for ONE Foundry request. Without this the SDK's own default
        # applies, which left the first call of every new chat unbounded -- the one
        # gap in the per-turn timeout budget.
        #
        # WORST CASE = FOUNDRY_HTTP_TIMEOUT x (1 + FOUNDRY_MAX_RETRIES). The OpenAI SDK
        # retries twice by default, so a 30s timeout would really mean 90s. Both are set
        # explicitly so the ceiling is visible and can be reasoned about.
        #
        # THIS BOUNDS THE CLIENT, NOT AN AGENT CALL. What the orchestrator itself asks of
        # Foundry is conversations.create() -- an empty conversation, normally sub-second.
        # An agent that runs a long call on the SHARED client (agents receive it, see
        # agents/base.py) must pass its own per-request timeout, for which it is given
        # AGENT_HTTP_TIMEOUT above; raising this value instead would slow down the
        # detection of a genuinely dead endpoint on every new chat.
        self.FOUNDRY_HTTP_TIMEOUT = _int(get("FOUNDRY_HTTP_TIMEOUT", "20"), 20)
        self.FOUNDRY_MAX_RETRIES = _int(get("FOUNDRY_MAX_RETRIES", "1"), 1)

        # -- Async job settings ---------------------------------------------
        # The flow starts the job and stores its job_id. Two things then advance it, both
        # through the same JobService.advance_job(): the FE's ~30s poll, and the reaper
        # (see workers/reaper.py). Whichever gets there first wins -- the compare-and-swap
        # in advance_after_job() makes that safe.
        #
        # Give up JOB_TIMEOUT_MINUTES after the job was TRIGGERED. Wall-clock, on purpose:
        # this replaced a poll counter, which only advanced when a browser was open. A
        # user who closed their tab never accumulated polls, so the old cap never fired
        # and the conversation stayed RUNNING forever. The old default (5 polls at ~30s
        # = 2.5 min) was also far below a real 3-15 minute Intune run.
        self.JOB_TIMEOUT_MINUTES = _int(get("JOB_TIMEOUT_MINUTES", "20"), 20)
        # Within ONE status check, retry a transient blip this many times.
        self.JOB_STATUS_RETRIES = _int(get("JOB_STATUS_RETRIES", "3"), 3)
        # How many stuck jobs one reaper sweep will handle. Caps how long a sweep can
        # run; a backlog simply drains over the next few sweeps.
        self.REAPER_BATCH_SIZE = _int(get("REAPER_BATCH_SIZE", "50"), 50)
        # The reaper only touches jobs nobody has looked at for this long. A browser
        # polling every ~30s keeps last_updated_at fresh, so an actively-watched job is
        # skipped entirely -- without this the reaper duplicated the browser's status
        # check on every single running job. 2 minutes = about four missed polls before
        # we assume the user has gone.
        self.REAPER_STALE_MINUTES = _int(get("REAPER_STALE_MINUTES", "2"), 2)
        # How often the reaper sweeps, in seconds. 300 = the "0 */5 * * * *" CRON the
        # Functions timer trigger used, kept identical so the behaviour did not change
        # with the hosting.
        #
        # On App Service this is an asyncio task inside the app (workers/reaper.py), not a
        # platform timer, which has two consequences worth knowing:
        #   * it needs ALWAYS ON. Without it the app idles out and nothing sweeps, so an
        #     abandoned job stays RUNNING forever -- the exact bug the reaper exists to fix.
        #   * it runs on EVERY instance, where the timer trigger took a lease and ran once.
        #     That is safe (advance_after_job is a compare-and-swap, and
        #     REAPER_STALE_MINUTES skips jobs a browser is watching) but it does mean N
        #     instances do N reads; raise the interval rather than the batch size if that
        #     ever shows up in DTU.
        self.REAPER_INTERVAL_SECONDS = _int(get("REAPER_INTERVAL_SECONDS", "300"), 300)
        # Off switch, for a local run or a debugging session where a background sweep
        # would be noise. warn_on_risky_config() complains loudly when it is off.
        self.REAPER_ENABLED = _flag(get("REAPER_ENABLED", "true"))

        # The diagnostics/troubleshoot agents keep the async contract they had as Function
        # Apps -- start() returns a job_id without waiting, status(job_id) reports -- and
        # app/domain/run_state.py turns their answers into {state, message, output}. There are no
        # start/status URLs any more: the kind -> agent mapping lives in JobRunner.
        #
        # NOTE: there is deliberately NO "simulate the jobs" switch and no fallback for an
        # agent that isn't ready -- JobRunner raises instead. The old JOBS_DUMMY flag
        # defaulted to true and faked a result whenever a URL was blank, so one missing
        # App Setting made the app tell real users "No issues found; Outlook profile
        # repaired" with nothing having run, and resolve their ServiceNow incident when
        # they confirmed. Tests inject a scripted stand-in for JobRunner.

        # How many outbound sockets to keep alive per host, for agents that make their own
        # HTTP calls (a device management API, a KB service). A pooled requests.Session
        # reuses connections instead of opening a fresh TCP+TLS one per call, which would
        # burn the instance's ~128 SNAT ports and show up under load as random timeouts.
        # The orchestrator itself no longer makes any outbound HTTP call: the agents it
        # used to POST to are now in-process.
        self.HTTP_POOL_MAXSIZE = _int(get("HTTP_POOL_MAXSIZE", "50"), 50)

    @property
    def use_sqlite(self) -> bool:
        """True when we're pointed at a local SQLite file instead of Azure SQL."""
        return bool(self.SQLITE_DB_PATH)

    def warn_on_risky_config(self) -> None:
        """Log loudly about settings that are fine locally but wrong in production.

        Called once from main.py at import. These are warnings, not hard failures, so
        local development still runs with an empty configuration -- but a production
        instance leaves an unmissable trail in Application Insights.
        """
        if self.DEFAULT_DEVICE_ID:
            logger.warning(
                "DEFAULT_DEVICE_ID is set (%s): EVERY diagnostic and troubleshoot job "
                "will be run against that one machine, whoever the user is. This is a "
                "stand-in until the device is resolved per user -- it must not stay set "
                "once real users are served.", self.DEFAULT_DEVICE_ID,
            )
        if self.use_sqlite:
            logger.warning(
                "SQLITE_DB_PATH is set (%s): state is on the per-instance temp disk "
                "and is lost on restart/scale-out. Use SQL_CONNECTION_STRING in "
                "production.", self.SQLITE_DB_PATH,
            )
        if not self.REAPER_ENABLED:
            logger.warning(
                "REAPER_ENABLED is false: a job whose user closes their browser will "
                "never be finished off -- its conversation stays in a RUNNING stage "
                "forever and its ticket keeps saying the work started. Local use only."
            )
        # Agents are no longer configured here -- there is no URL to check for. Whether an
        # agent is ready is answered by the agent: an unimplemented one raises from its
        # own _call (see agents/base.py), which the flow turns into the user-facing
        # fallback rather than a wrong answer.
        if not self.AZURE_FOUNDRY_PROJECT_ENDPOINT:
            logger.warning("AZURE_FOUNDRY_PROJECT_ENDPOINT not configured")


# The process-wide instance every layer reads. Tests build their own Settings and
# inject it rather than mutating this one.
settings = Settings()
