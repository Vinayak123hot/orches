####################################################################################################
# Project name      : IT Support Orchestrator API -- Azure App Service (FastAPI)                   #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# The ONE place environment configuration is read, parsed and sanity-checked.                      #
#   1. Configure root logging exactly once, before any other module can log.                       #
#   2. Parse every env var / App Service Application Setting onto the Settings class.              #
#   3. Expose the process-wide `settings` and `logger` singletons the whole app reads.             #
#   4. warn_on_risky_config(): shout at startup about values that are fine locally, wrong in prod. #
#                                                                                                  #
# Source:-                                                                                         #
#   - os.environ supplies the values; on App Service, Application Settings arrive as plain env     #
#       vars, so the same code reads a local .env and a deployed slot with no branching.           #
#   - python-dotenv (load_dotenv) loads a local .env file for development runs only.               #
#   - logging + sys configure the root logger to write plain JSON records to STDOUT.               #
#   - typing (Mapping / Optional) types the injectable `env` argument used by tests.               #
####################################################################################################
"""Settings (environment / App Settings) and the shared logger.

What this module is:
    - The Settings class, one attribute per environment-derived value, read once into the
      module-level `settings` singleton; plus `logger`, the shared application logger.

Why this exists:
    - Every environment-derived value lives on Settings, so "where did this value come
      from?" has exactly one answer. Domain constants that are NOT configuration (stage
      names, agent vocabularies, fixed prompts) live in constants.py instead.
    - Why a class instead of ~20 module-level os.getenv() calls:
        * a test can build Settings({"REAPER_ENABLED": "false", ...}) instead of
          reimporting the module to change one value;
        * parsing and defaults live in one place, so "was this an int or a string?" has
          one answer;
        * warn_on_risky_config() gives us a single startup check -- which is what catches a
          misconfiguration instead of it silently defaulting and telling real users their
          machine was repaired.

Security and production notes:
    1. No secrets or URLs are hardcoded. Locally the values come from a .env file (loaded
       below); on App Service they are Application Settings, which arrive as plain
       environment variables -- so the same code reads both. See .env.example for the list.
    2. The _int/_flag/_csv parsers never raise on junk input: a malformed App Setting logs a
       warning and falls back to the default, because a typo in one setting must not stop
       the whole app from starting.
    3. warn_on_risky_config() is intentionally warnings-only, not hard failures, so local
       development still runs with an empty configuration -- but a production instance
       leaves an unmissable trail in the log stream.
"""

# ============================================ Imports =============================================
import logging  # Configure the root logger and obtain the shared application logger                 # stdlib logging
import os  # Read the process environment (App Settings arrive here as plain env vars)               # stdlib os
import sys  # sys.stdout is the stream every log record is written to                                # stdlib sys
from typing import Mapping, Optional  # Types for the injectable `env` mapping used by tests         # stdlib typing

from dotenv import load_dotenv  # Loads a local .env file for development runs only                  # python-dotenv

# ===================================== Application bootstrap ======================================
# Local development only. On App Service there is no .env file, so this is a silent no-op and
# the values come from Application Settings instead.
load_dotenv()  # Populate os.environ from .env if one exists next to the project root                # load .env

# The ONE place root logging is configured. This module is imported by nearly everything, so it
# always runs first -- and logging.basicConfig does nothing once a handler exists, which makes
# any later call (LogFactory's, for instance) a silent no-op. One owner, and it has to be the
# module that loads earliest, or the startup warnings below would be emitted through an
# unconfigured logger.
#
# format="%(message)s"  the structured logger writes a whole JSON record as the message, so a
#                       prefix would leave stdout holding lines that are not JSON -- and it
#                       would duplicate the timestamp, level and component name that are
#                       already inside the record.
# stream=sys.stdout     without it Python defaults to STDERR, so every line including INFO goes
#                       out on the error stream and log collectors flag routine messages as
#                       errors.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),  # Read directly: Settings does not exist yet              # log level
    format="%(message)s",  # No prefix, so a structured JSON record stays valid JSON                 # bare message
    stream=sys.stdout,  # STDOUT, so INFO lines are not collected as errors                          # to stdout
)
logger = logging.getLogger("orchestrator_api")  # The shared logger every module imports             # app logger


# ======================================== Setting parsers =========================================
def _csv(raw: str) -> list:
    """Split a comma-separated App Setting into a clean list.

    What this function does:
        - Splits on commas, strips surrounding whitespace from each part, and drops empties.

    Why it exists:
        - App Settings are always strings, and a hand-edited list routinely arrives with
          stray spaces or a trailing comma. Dropping empties means "a,b," yields two items
          rather than an empty third origin that would never match anything.

    Args:
        raw: The raw setting value, e.g. "http://a,http://b".

    Returns:
        The list of non-empty, stripped parts.

    Example:
        >>> _csv("http://a, http://b,")
        ['http://a', 'http://b']
    """
    return [part.strip() for part in raw.split(",") if part.strip()]  # Strip, then drop empties     # split csv


def _flag(raw: str) -> bool:
    """Parse a boolean App Setting (they arrive as strings).

    What this function does:
        - Treats "1", "true", "yes" and "on" (any case, surrounding space ignored) as True
          and everything else as False.

    Why it exists:
        - bool("false") is True in Python, so reading a flag straight from the environment
          is a live bug. Several spellings are accepted because the Azure portal, the CLI
          and .env files are all edited by hand.

    Security and production notes:
        1. Unrecognised input is False, so a typo DISABLES a feature rather than silently
           enabling one. Note the callers choose the default separately -- REAPER_ENABLED
           defaults to "true", so a typo there is caught by warn_on_risky_config().

    Args:
        raw: The raw setting value.

    Returns:
        True only for a recognised truthy spelling.

    Example:
        >>> _flag(" TRUE "), _flag("false"), _flag("banana")
        (True, False, False)
    """
    return raw.strip().lower() in ("1", "true", "yes", "on")  # Recognised truthy spellings          # parse flag


def _int(raw: str, default: int) -> int:
    """Parse an int App Setting, falling back rather than crashing on junk.

    What this function does:
        - Returns int(raw), or logs a warning and returns `default` if raw is None or not
          a valid integer.

    Why it exists:
        - A typo in one numeric App Setting must not stop the app from starting. The
          warning is what makes the fallback discoverable instead of mysterious.

    Args:
        raw: The raw setting value, possibly None or malformed.
        default: The value to use when raw cannot be parsed.

    Returns:
        The parsed integer, or `default`.

    Example:
        >>> _int("45", 10), _int("abc", 10)
        (45, 10)
    """
    try:  # Attempt a straight int() parse of the raw setting                                        # parse try
        return int(raw)  # Well-formed setting -> use it                                             # return int
    except (TypeError, ValueError):  # None (unset) or junk (not a number)                           # bad value
        logger.warning("expected an integer setting, got %r; using %s", raw, default)  # Make it discoverable  # warn
        return default  # Fall back so one typo cannot stop the app from starting                    # use default


# ============================================ Settings ============================================
class Settings:
    """All environment-derived configuration, parsed once.

    What this class is:
        - A flat bag of parsed settings grouped by concern: transport, language policy,
          flow policy, the state store, the SQL pool, Foundry, async jobs and logging.

    Why it exists:
        - `env` defaults to os.environ; pass a plain dict in tests to build a Settings with
          whatever values that test needs, instead of mutating the process environment.

    Security and production notes:
        1. Nothing here is a secret in itself, but SQL_CONNECTION_STRING is: it must come
           from an App Setting or Key Vault reference and must never be logged. Prefer a
           managed identity and leave the credential settings empty where possible.
        2. Several defaults are safe locally and WRONG in production (SQLITE_DB_PATH,
           DEFAULT_DEVICE_ID). warn_on_risky_config() is the startup check that catches
           them; read it as the companion to this constructor.

    Example:
        >>> Settings({"API_PREFIX": "/v2"}).API_PREFIX
        '/v2'
    """

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        """Read and parse every setting once, from `env` or the process environment.

        What this method does:
            - Binds `get` to the chosen mapping's .get, then parses each setting into a
              typed attribute using the _csv / _flag / _int helpers above.

        Why it exists:
            - Parsing once at construction means every reader gets the same typed value,
              and a test can inject a dict rather than reimporting the module.

        Security and production notes:
            1. Every value has a default, so the app starts with an empty environment.
               That is what makes local development possible -- and exactly why
               warn_on_risky_config() exists to flag the defaults that are unsafe in prod.

        Args:
            env: Mapping to read from. Defaults to os.environ; tests pass a plain dict.

        Returns:
            None.

        Example:
            >>> Settings({"MAX_NONACTIONABLE": "5"}).MAX_NONACTIONABLE
            5
        """
        get = (env if env is not None else os.environ).get  # One accessor for either source         # bind get

        # -- Frontend / transport -------------------------------------------
        # Browsers require explicit origins whenever credentials are sent; main.py drops
        # credentials rather than ship the invalid "*" + credentials combination.
        self.CORS_ORIGINS = _csv(  # Allowed browser origins, as a clean list                        # cors list
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
        self.API_PREFIX = get("API_PREFIX", "/api")  # Mount point for every API route               # api prefix
        # Seconds an agent gets for ONE call, passed to every agent in agents/ so the
        # per-turn timeout budget is set in one place rather than seven.
        #
        # There are no *_AGENT_URL settings any more. The agents are not Function Apps we
        # POST to -- they are classes in agents/<name>/, constructed in deps.py and called
        # directly (see agents/__init__.py). Whatever an agent needs to reach its own
        # backend (a Foundry agent name, a Graph permission, a KB index) is that agent's
        # configuration and is read in its own folder, which is why nothing here names an
        # individual agent.
        self.AGENT_HTTP_TIMEOUT = _int(get("AGENT_HTTP_TIMEOUT", "120"), 120)  # Per-agent-call budget  # agent timeout

        # -- Language policy -------------------------------------------------
        # The language the flow works in internally: the inbound message is translated
        # into it before the yes/no checks and the other agents run, and outgoing
        # messages are translated back out of it. Also the language assumed when
        # detection is unavailable, uncertain or unsupported -- and outgoing messages are
        # NOT translated when the detected language equals this one.
        self.DEFAULT_LANG = get("DEFAULT_LANG", "en")  # The flow's internal working language        # default lang

        # -- Flow policy ----------------------------------------------------
        # How many greeting / non-IT turns are re-prompted before the conversation is ended.
        # One shared counter covers both kinds, so a user alternating between them still
        # hits the cap.
        self.MAX_NONACTIONABLE = _int(get("MAX_NONACTIONABLE", "2"), 2)  # Re-prompt cap             # nonactionable

        # TEMPORARY -- the Intune device every job is targeted at.
        #
        # The conversation carries user_id but nothing about a machine, so the
        # orchestrator genuinely cannot know which device to remediate. Until that is
        # settled (either the frontend supplies it, or the diagnostics service resolves
        # user -> device via Graph), every diagnostic and troubleshoot job runs against
        # this one test device. warn_on_risky_config() says so loudly at startup, because
        # remediating the wrong machine is the worst failure available here.
        self.DEFAULT_DEVICE_ID = get(  # STAND-IN: every job targets this one machine                # device id
            "DEFAULT_DEVICE_ID", "d57cc3db-d2ad-42c3-a855-476359ac0aac"
        )

        # -- State store ----------------------------------------------------
        # If SQLITE_DB_PATH is set, use a LOCAL SQLite file instead of Azure SQL.
        # Handy for testing on a machine that can't reach Azure SQL. Leave it empty
        # to use the real Azure SQL connection (SQL_CONNECTION_STRING).
        self.SQLITE_DB_PATH = get("SQLITE_DB_PATH", "")  # Set -> local file DB, no Azure SQL        # sqlite path
        self.SQL_CONNECTION_STRING = get("SQL_CONNECTION_STRING", "")  # SECRET: never log this      # sql conn

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
        self.SQL_POOL_MAX_SIZE = _int(get("SQL_POOL_MAX_SIZE", "45"), 45)  # Per-process pool ceiling  # pool size
        # Seconds a connection may sit UNUSED before it is closed and dropped
        # from the pool -- housekeeping, so we don't hold connections open all
        # night. This is NOT a "wait for a free connection" timeout.
        self.SQL_POOL_IDLE_TIMEOUT = _int(get("SQL_POOL_IDLE_TIMEOUT", "300"), 300)  # Idle reap, not a wait  # idle timeout

        # -- Azure AI Foundry ------------------------------------------------
        self.AZURE_FOUNDRY_PROJECT_ENDPOINT = get(  # Project data-plane endpoint for the SDK        # foundry url
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
        self.FOUNDRY_HTTP_TIMEOUT = _int(get("FOUNDRY_HTTP_TIMEOUT", "20"), 20)  # Per-request ceiling  # foundry timeout
        self.FOUNDRY_MAX_RETRIES = _int(get("FOUNDRY_MAX_RETRIES", "1"), 1)  # Retries, so worst case is visible  # foundry retries

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
        self.JOB_TIMEOUT_MINUTES = _int(get("JOB_TIMEOUT_MINUTES", "20"), 20)  # Wall-clock give-up  # job timeout
        # Within ONE status check, retry a transient blip this many times. Safe because a
        # status check is a READ -- retrying it cannot start a second remediation.
        self.JOB_STATUS_RETRIES = _int(get("JOB_STATUS_RETRIES", "3"), 3)  # Retries per status check  # status retries
        # How many stuck jobs one reaper sweep will handle. Caps how long a sweep can
        # run; a backlog simply drains over the next few sweeps.
        self.REAPER_BATCH_SIZE = _int(get("REAPER_BATCH_SIZE", "50"), 50)  # Jobs handled per sweep  # batch size
        # The reaper only touches jobs nobody has looked at for this long. A browser
        # polling every ~30s keeps last_updated_at fresh, so an actively-watched job is
        # skipped entirely -- without this the reaper duplicated the browser's status
        # check on every single running job. 2 minutes = about four missed polls before
        # we assume the user has gone.
        self.REAPER_STALE_MINUTES = _int(get("REAPER_STALE_MINUTES", "2"), 2)  # "User has gone" threshold  # stale mins
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
        self.REAPER_INTERVAL_SECONDS = _int(get("REAPER_INTERVAL_SECONDS", "300"), 300)  # Sweep period  # sweep secs
        # Off switch, for a local run or a debugging session where a background sweep
        # would be noise. warn_on_risky_config() complains loudly when it is off.
        self.REAPER_ENABLED = _flag(get("REAPER_ENABLED", "true"))  # Default ON in every environment  # reaper on

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
        self.HTTP_POOL_MAXSIZE = _int(get("HTTP_POOL_MAXSIZE", "50"), 50)  # Keep-alive sockets per host  # http pool

        # -- Structured logging / Event Hub (app/event_hub) -------------------
        # LOG_LEVEL is read at import for basicConfig above; kept here too so the log
        # factory can use the same value without re-reading the environment.
        self.LOG_LEVEL = get("LOG_LEVEL", "INFO")  # Same value basicConfig already used             # log level
        # Forward every structured log line to Azure Event Hub, for Splunk. OFF by
        # default, and it needs the namespace and hub name as well as the flag -- so this
        # ships disabled and enabling it is an App Setting rather than a deploy.
        #
        # Before it is ever switched on: our log lines must stop carrying user emails and
        # function keys. These records leave the tenant, and the standard this follows
        # says "never secrets or PII" -- see the module docstring in event_hub.
        self.EVENTHUB_ENABLED = _flag(get("EVENTHUB_ENABLED", "false"))  # Ships OFF; see the PII note  # eventhub on
        self.EVENTHUB_NAMESPACE = get("EVENTHUB_NAMESPACE", "")  # Fully qualified, e.g. ns.servicebus.windows.net  # namespace
        self.EVENTHUB_NAME = get("EVENTHUB_NAME", "")  # The hub (topic) records are sent to         # hub name

    # ========================================= Derived values =====================================
    @property
    def use_sqlite(self) -> bool:
        """True when we're pointed at a local SQLite file instead of Azure SQL."""
        return bool(self.SQLITE_DB_PATH)  # A non-empty path is the whole switch                     # sqlite?

    # ========================================= Startup check ======================================
    def warn_on_risky_config(self) -> None:
        """Log loudly about settings that are fine locally but wrong in production.

        What this method does:
            - Emits one WARNING per risky value: the shared test device, the local SQLite
              store, a disabled reaper, and a missing Foundry endpoint.

        Why it exists:
            - Called once from the lifespan in main.py, per worker process, at a point that
              appears in the log stream -- rather than at import, where the platform may
              swallow it. These are warnings, not hard failures, so local development still
              runs with an empty configuration.

        Security and production notes:
            1. DEFAULT_DEVICE_ID is the most dangerous of the four: while it is set, every
               remediation runs against ONE machine regardless of who is chatting.
            2. Agents are no longer configured here -- there is no URL to check for. Whether
               an agent is ready is answered by the agent: an unimplemented one raises from
               its own _call (see agents/base.py), which the flow turns into the user-facing
               fallback rather than a wrong answer.
            3. Only names and paths are logged, never SQL_CONNECTION_STRING -- these lines
               can be forwarded off-tenant to Splunk.

        Returns:
            None.

        Example:
            >>> Settings({"REAPER_ENABLED": "false"}).warn_on_risky_config()  # doctest: +SKIP
        """
        if self.DEFAULT_DEVICE_ID:  # A shared target device is set -- the worst failure here        # device set?
            logger.warning(
                "DEFAULT_DEVICE_ID is set (%s): EVERY diagnostic and troubleshoot job "
                "will be run against that one machine, whoever the user is. This is a "
                "stand-in until the device is resolved per user -- it must not stay set "
                "once real users are served.", self.DEFAULT_DEVICE_ID,
            )
        if self.use_sqlite:  # Local file DB: state does not survive a restart or scale-out          # sqlite?
            logger.warning(
                "SQLITE_DB_PATH is set (%s): state is on the per-instance temp disk "
                "and is lost on restart/scale-out. Use SQL_CONNECTION_STRING in "
                "production.", self.SQLITE_DB_PATH,
            )
        if not self.REAPER_ENABLED:  # Nothing will finish off an abandoned job                      # reaper off?
            logger.warning(
                "REAPER_ENABLED is false: a job whose user closes their browser will "
                "never be finished off -- its conversation stays in a RUNNING stage "
                "forever and its ticket keeps saying the work started. Local use only."
            )
        if not self.AZURE_FOUNDRY_PROJECT_ENDPOINT:  # No endpoint -> the first agent call fails     # foundry set?
            logger.warning("AZURE_FOUNDRY_PROJECT_ENDPOINT not configured")  # Warn, don't block startup  # warn

# ======================================== Module singleton ========================================
# The process-wide instance every layer reads. Tests build their own Settings and inject it
# rather than mutating this one, so one test cannot leak configuration into the next.
settings = Settings()  # Parsed once per worker process at import time                               # singleton
