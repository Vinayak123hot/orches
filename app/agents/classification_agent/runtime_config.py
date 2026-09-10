####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
#   1. Load the application's config.yaml and validate this agent's section into typed settings.   #
#   2. Define every config shape this agent needs (Foundry agent, KB, retry, cost, logging).       #
#   3. Overlay a small allowlist of non-secret environment variables onto the loaded values.       #
#                                                                                                  #
# Scope:                                                                                           #
#   This file carries the settings that select and tune the AGENT: which Foundry agent to invoke,  #
#   its call budgets, the knowledge-base index, and the retry, cost and logging policy. The        #
#   Foundry connection is owned by the hosting application and injected at construction.           #
#                                                                                                  #
# Source:-                                                                                         #
#   - os is read for the environment-variable override allowlist.                                  #
#       - os.environ.get:- reads each override value; None means the env var is unset (skipped).   #
#   - pathlib.Path anchors the agent folder (data files) and the application config file.          #
#   - yaml.safe_load parses the application's config.yaml.                                         #
#   - pydantic.BaseModel / Field type every config boundary and supply safe field defaults.        #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of annotations (PEP 563) for forward refs  # future import

import os  # Read environment variables used to override selected config values                    # stdlib os
from pathlib import Path  # Filesystem path handling                                                # stdlib pathlib

import yaml  # Parse the YAML configuration file                                                    # yaml parser
from pydantic import BaseModel, Field  # Base class for typed models and helper for field defaults   # boundary models

# The folder holding THIS agent's own modules and data files (the KB index lives beside this file).
AGENT_ROOT: Path = Path(__file__).resolve().parent  # Absolute path to this agent's package folder   # agent root

# The application-level configuration file, three levels up: app/agents/<agent>/ -> app/.
CONFIG_PATH: Path = Path(__file__).resolve().parents[2] / "config.yaml"  # app/config.yaml           # config path

# The key under which this agent's settings live inside the shared application config file. Every
# agent owns one top-level section, so one file can serve several agents without them colliding.
CONFIG_SECTION_NAME: str = "classification_agent"  # This agent's section in config.yaml             # config section

# Environment variable -> (config section, field). Lets operators override a small set
# of critical, non-secret settings via Application Settings WITHOUT redeploying the YAML.
_ENV_OVERRIDES: dict[str, tuple[str, str]] = {  # Map of env var name to the (section, field) it overrides  # override table
    "CLASSIFICATION_AGENT_NAME": ("foundry", "agent_name"),  # Override the Foundry agent name       # agent name
    "CLASSIFICATION_AGENT_VERSION": ("foundry", "agent_version"),  # Override the pinned agent version  # agent version
}


# ========================================= Config models =========================================
class EventHubConfig(BaseModel):  # Typed model for Event Hub log-forwarding settings
    """Event Hub log-forwarding settings.

    What this model is:
        - The typed shape of the optional Event Hub log sink; disabled by default so no forwarding
          happens until an operator opts in via config.

    Security and production notes:
        1. This model carries the namespace and hub name; the emitter authenticates with Entra ID
           via DefaultAzureCredential.
        2. Forwarding stays OFF (enabled=False) unless explicitly enabled in the YAML.
    """

    enabled: bool = False  # Whether Event Hub forwarding is enabled (default off)                   # forwarding toggle
    fully_qualified_namespace: str = ""  # Event Hubs namespace host (default empty)                 # namespace host
    event_hub_name: str = ""  # Target Event Hub name (default empty)                                # hub name


class KnowledgeBaseConfig(BaseModel):  # Typed model for how a search result is shaped for the agent
    """How much of a search result reaches the agent.

    What this model is:
        - The typed shape of the limit applied to every search: how many articles are handed over.

    Why it is configurable:
        - It trades the agent's ability to tell articles apart against tokens and latency. A search
          can return dozens of articles and one turn can run several searches, so this is the
          setting that decides what a turn costs. Tune it against real searches.
    """

    max_candidates: int = 15  # Most articles one search hands to the agent                          # candidate cap


class ServiceNowSearchConfig(BaseModel):  # Typed model for the knowledge search call policy
    """Call policy for the ServiceNow knowledge search endpoint.

    What this model is:
        - The typed shape of the non-secret settings for the search call: how long one request may
          take, how many connections to keep alive, and whether the certificate is verified.

    Where the credentials are:
        - The endpoint URL, bearer token, registration id and account are read from the
          environment by load_servicenow_credentials, so no secret is ever written in the YAML.

    Security and production notes:
        1. verify_tls should stay True. Turning it off disables certificate checking and is only
           ever appropriate against a test endpoint on a private network.
    """

    request_timeout_seconds: int = 20  # Hard timeout for one search request, in seconds             # call timeout
    pool_maxsize: int = 10  # Outbound connections kept alive for reuse                              # pool size
    verify_tls: bool = True  # Whether the server certificate is verified                            # verify tls


class RetryConfig(BaseModel):  # Typed model for retry/backoff settings
    """Retry/backoff settings with optional per-operation overrides.

    What this model is:
        - The typed shape of the exponential-backoff policy applied to every remote call, with an
          optional per-operation attempt override map.

    Why this exists:
        - Remote calls fail transiently and must retry with bounded backoff rather than failing on
          the first blip; centralising the policy keeps every call site consistent.
    """

    default_max_attempts: int = 3  # Default maximum attempts per remote call                        # attempt cap
    base_delay_seconds: float = 0.5  # Base backoff delay in seconds                                 # base delay
    max_delay_seconds: float = 8.0  # Maximum backoff delay in seconds                               # delay ceiling
    per_tool_max_attempts: dict[str, int] = Field(default_factory=dict)  # Per-operation attempt overrides  # per-op overrides

    def max_attempts_for(self, operation_name: str) -> int:  # Return max attempts for an operation, defaulting when absent
        """Return the max attempts for an operation, falling back to the default.

        Args:
            operation_name: The operation name (e.g. 'foundry_agent').

        Returns:
            The configured max attempts for that operation.

        Example:
            >>> RetryConfig().max_attempts_for("foundry_agent")
            3
        """
        return self.per_tool_max_attempts.get(operation_name, self.default_max_attempts)  # Override or default  # per-op lookup


class ModelPrice(BaseModel):  # Typed model for one model's per-token prices
    """Per-token input/output price for one model.

    What this model is:
        - The typed price pair (input, output) for a single model, used by the cost tracker to price
          token usage.
    """

    input_price: float = 0.0  # Price per input token (default 0.0)                                  # input price
    output_price: float = 0.0  # Price per output token (default 0.0)                                # output price


class CostConfig(BaseModel):  # Typed model for cost-tracking prices keyed by model
    """Cost-tracking prices keyed by model name.

    What this model is:
        - The typed lookup table mapping a model name to its ModelPrice, letting the cost tracker
          price each response's token usage.
    """

    prices: dict[str, ModelPrice] = Field(default_factory=dict)  # Map of model name to its ModelPrice  # price table


class LoggingConfig(BaseModel):  # Typed model for logging settings
    """Logging settings.

    What this model is:
        - The typed shape of the structured-logging settings; controls the minimum level emitted by
          this agent's loggers.
    """

    log_level: str = "INFO"  # Minimum log level to emit (default INFO)                              # log level


class FoundryConfig(BaseModel):  # Typed model for the Foundry agent selection and call budget
    """Foundry agent settings: which agent to invoke and how far one turn may go.

    What this model is:
        - The typed shape of the values that select the pre-created Foundry agent by NAME and
          VERSION, and the per-call / per-turn budgets that bound one classification turn.

    Where the connection comes from:
        - The hosting application constructs and authenticates the Foundry project client once per
          worker and injects it into this agent, so this model configures the agent selection and
          the call budgets only.

    Security and production notes:
        1. agent_version pins a concrete version when set to a number. Leave it blank (or set it to
           'default') to let Foundry resolve the agent's own default version.
    """

    agent_name: str = ""  # Name of the pre-created Foundry agent to invoke                          # agent name
    agent_version: str = ""  # Concrete version to pin (e.g. '9'); blank or 'default' lets Foundry decide  # agent version
    agent_model_name: str = ""  # Model name used for cost lookup/logging of response token usage    # cost model name
    request_timeout_seconds: int = 60  # Hard timeout for ONE Foundry call, in seconds               # call timeout
    max_search_rounds: int = 6  # Max knowledge-base fetches per turn before a no_match handoff      # search budget


class AgentSettings(BaseModel):  # Top-level typed model aggregating this agent's config sections
    """The full validated configuration for the classification agent.

    What this model is:
        - The single aggregate boundary that every config section validates into; the turn service
          reads its typed fields rather than raw YAML.

    Why this exists:
        - So a malformed or missing setting fails loudly at construction with a schema error,
          rather than surfacing as a confusing failure part-way through a live conversation.
    """

    knowledge_base: KnowledgeBaseConfig = Field(default_factory=KnowledgeBaseConfig)  # KB source settings  # kb section
    servicenow_search: ServiceNowSearchConfig = Field(default_factory=ServiceNowSearchConfig)  # Search call policy  # search section
    foundry: FoundryConfig = Field(default_factory=FoundryConfig)  # Foundry agent settings          # foundry section
    event_hub: EventHubConfig = Field(default_factory=EventHubConfig)  # Event Hub settings           # event hub section
    retry: RetryConfig = Field(default_factory=RetryConfig)  # Retry settings                        # retry section
    cost: CostConfig = Field(default_factory=CostConfig)  # Cost settings                            # cost section
    logging: LoggingConfig = Field(default_factory=LoggingConfig)  # Logging settings                # logging section


# ============================================= Loader ============================================
def load_settings() -> AgentSettings:  # Load and validate this agent's section of the application config
    """Load config.yaml from the application root and validate this agent's section.

    What this function is:
        - The single entry point that reads the shared application configuration file, selects this
          agent's section, overlays the non-secret environment overrides, and returns validated
          settings.

    Why this exists:
        - It keeps config loading in one typed place so the turn service never parses raw YAML and
          every value is schema-checked before use.

    Security and production notes:
        1. The file is read as UTF-8 from a path derived from this module's location, so the agent
           always reads the configuration shipped with it.

    Args:
        None.

    Returns:
        The validated AgentSettings for this agent.

    Raises:
        FileNotFoundError: If the application config file does not exist.

    Example:
        >>> settings = load_settings()  # doctest: +SKIP
        >>> settings.foundry.agent_name  # doctest: +SKIP
        'clasification-agent'
    """
    if not CONFIG_PATH.exists():  # Verify the application config file is present                    # existence check
        raise FileNotFoundError(  # Raise a clear error naming the file we expected to find          # missing file
            f"Application config file not found: {CONFIG_PATH}"  # Error with the resolved path      # error message
        )
    with CONFIG_PATH.open("r", encoding="utf-8") as config_file:  # Open the config file as UTF-8    # open utf-8
        config_document = yaml.safe_load(config_file) or {}  # Parse YAML content (empty file -> {})  # parse yaml
    section_data = config_document.get(CONFIG_SECTION_NAME) or {}  # Select this agent's section     # take section
    if not isinstance(section_data, dict):  # A present-but-malformed section cannot be validated    # shape guard
        raise ValueError(  # Fail fast rather than silently falling back to defaults                 # bad section
            f"Config section '{CONFIG_SECTION_NAME}' in {CONFIG_PATH} must be a mapping."  # Message  # error message
        )
    section_data = _apply_env_overrides(section_data)  # Overlay any environment-variable overrides  # apply overrides
    return AgentSettings.model_validate(section_data)  # Validate the section and return it          # validate + return


class ServiceNowCredentials(BaseModel):  # Typed model for the search endpoint's connection details
    """Connection details for the ServiceNow knowledge search endpoint.

    What this model is:
        - The typed shape of the values read from the environment: where to send a search, how to
          authenticate, and the fixed parameters every request carries.

    Why these live in the environment:
        - The token and registration id are secrets, and the environment is the one place they can
          be supplied without being written into a file that is committed. Locally they come from
          this folder's .env; on App Service they are Application Settings, ideally Key Vault
          references, which arrive as ordinary environment variables.

    Security and production notes:
        1. Nothing here is ever logged. The client sends the token as a header and logs only the
           status code and the number of records returned.
        2. user_id is the account the search runs as. The agent is not given the end user's own
           identity, so every search is attributed to this account until that value is passed
           through to the agent.
    """

    base_url: str  # Endpoint URL, without a query string                                            # base url
    registration_id: str  # Identifies this integration to the service                               # registration
    user_id: str  # The account the search runs as                                                   # user id
    req_type: str = "search"  # Fixed 'reqType' query parameter                                      # req type
    search_type: str = "knowledge"  # Fixed 'searchType' query parameter                             # search type

    # --- How the bearer token is obtained ---------------------------------------------------------
    # Either the four oauth_* values are set, and a token is requested as needed, or bearer_token
    # carries one directly. load_servicenow_credentials checks that one of the two is complete.
    oauth_token_url: str = ""  # Endpoint a token is requested from                                  # token url
    oauth_client_id: str = ""  # The application's own identifier                                    # client id
    oauth_client_secret: str = ""  # The application's own secret                                    # client secret
    oauth_scope: str = ""  # The scope a token is requested for                                      # scope
    bearer_token: str = ""  # A token supplied directly                                              # static token

    @property
    def uses_oauth(self) -> bool:  # True when a token is requested rather than supplied
        """Report whether a token is requested from the identity provider.

        Returns:
            True when all four oauth_* values are set.

        Example:
            >>> ServiceNowCredentials(base_url="u", registration_id="r", user_id="a").uses_oauth
            False
        """
        return all((self.oauth_token_url, self.oauth_client_id,  # Every part must be present        # all set?
                    self.oauth_client_secret, self.oauth_scope))  # for a token to be requested      # all set?


def load_servicenow_credentials() -> ServiceNowCredentials:  # Read the search endpoint's details from the environment
    """Read the knowledge search connection details from the environment.

    What this function is:
        - The single reader of the search endpoint's credentials. It loads this folder's .env when
          one is present, then validates the four required values.

    Why the .env is loaded here:
        - The application loads its own .env from the repository root. This agent's credentials sit
          beside its code, so this is the one place that file is read. Values already present in
          the environment always win, so App Service settings are never overwritten by a stray file.

    Security and production notes:
        1. A missing or blank value raises with the NAME of the setting only - never a value, and
           never a partial token.
        2. The returned object is held in memory by the search client for the life of the worker
           and is not written anywhere.

    Args:
        None.

    Returns:
        The validated ServiceNowCredentials.

    Raises:
        ValueError: If any required value is missing or blank.

    Example:
        >>> credentials = load_servicenow_credentials()  # doctest: +SKIP
        >>> credentials.req_type  # doctest: +SKIP
        'search'
    """
    try:  # Load this folder's .env when python-dotenv is installed                                  # try dotenv
        from dotenv import load_dotenv  # Imported here so the package stays optional                # lazy import

        # override=False: a value already in the environment (an Application Setting) always wins.
        load_dotenv(AGENT_ROOT / ".env", override=False)  # Read the .env beside this agent's code   # load env
    except ImportError:  # python-dotenv is absent; the real environment is used as-is               # no dotenv
        pass  # Continue with whatever the process already has                                       # carry on

    def read(name: str, default: str = "") -> str:  # Read one setting, treating a placeholder as unset
        """Return an environment value, with an unfilled placeholder treated as absent."""
        raw = os.environ.get(name, default).strip()  # The value as set                              # read value
        return "" if raw.startswith("<") and raw.endswith(">") else raw  # Placeholder counts as unset  # unfilled?

    values = {  # Read each setting from the environment                                             # read env
        "base_url": read("SERVICENOW_API_BASE_URL"),  # Endpoint URL                                 # base url
        "registration_id": read("SERVICENOW_API_REGISTRATION_ID"),  # Identifies this integration    # registration
        "user_id": read("SERVICENOW_API_USER_ID"),  # Account to search as                           # user id
        "req_type": read("SERVICENOW_API_REQ_TYPE", "search"),  # Fixed parameter                    # req type
        "search_type": read("SERVICENOW_API_SEARCH_TYPE", "knowledge"),  # Fixed parameter           # search type
        "oauth_token_url": read("SERVICENOW_OAUTH_TOKEN_URL"),  # Token endpoint                     # token url
        "oauth_client_id": read("SERVICENOW_OAUTH_CLIENT_ID"),  # Application identifier             # client id
        "oauth_client_secret": read("SERVICENOW_OAUTH_CLIENT_SECRET"),  # Application secret         # client secret
        "oauth_scope": read("SERVICENOW_OAUTH_SCOPE"),  # Scope a token is requested for             # scope
        "bearer_token": read("SERVICENOW_API_BEARER_TOKEN"),  # A token supplied directly            # static token
    }

    required_settings = {  # The three the search itself cannot run without                          # required map
        "SERVICENOW_API_BASE_URL": values["base_url"],  # Endpoint URL                               # base url
        "SERVICENOW_API_REGISTRATION_ID": values["registration_id"],  # Registration id              # registration
        "SERVICENOW_API_USER_ID": values["user_id"],  # Account to search as                         # user id
    }

    # The token is either requested or supplied. Requesting one is preferred, since a supplied token
    # expires within the hour and has to be replaced by hand.
    oauth_settings = {  # Every part needed to request a token                                       # oauth map
        "SERVICENOW_OAUTH_TOKEN_URL": values["oauth_token_url"],  # Token endpoint                   # token url
        "SERVICENOW_OAUTH_CLIENT_ID": values["oauth_client_id"],  # Application identifier           # client id
        "SERVICENOW_OAUTH_CLIENT_SECRET": values["oauth_client_secret"],  # Application secret       # client secret
        "SERVICENOW_OAUTH_SCOPE": values["oauth_scope"],  # Scope a token is requested for           # scope
    }
    oauth_present = [name for name, value in oauth_settings.items() if value]  # What was supplied   # present
    if oauth_present and len(oauth_present) < len(oauth_settings):  # Started but not finished       # partial?
        missing_oauth = sorted(set(oauth_settings) - set(oauth_present))  # What is still needed      # missing
        raise ValueError(  # A half-filled set would silently fall back to a supplied token           # raise
            "Incomplete token settings: " + ", ".join(missing_oauth) + ". "  # Which names            # names
            "Set all four to request a token, or none to use SERVICENOW_API_BEARER_TOKEN."  # How     # how
        )
    if not oauth_present and not values["bearer_token"]:  # Neither way of getting a token is set     # no token?
        required_settings["SERVICENOW_OAUTH_* (or SERVICENOW_API_BEARER_TOKEN)"] = ""  # Report it    # add to missing
    # A value still carrying its <angle-bracket> placeholder counts as unset. Without this a
    # half-filled .env passes the check and the first search fails against a hostname of
    # "<host>", which reads as a network fault rather than the missing setting it really is.
    missing = sorted(  # Report the names, never the values                                          # find missing
        name for name, value in required_settings.items()  # Each required setting                   # each setting
        if not value or (value.startswith("<") and value.endswith(">"))  # Blank, unset or a placeholder  # unusable?
    )
    if missing:  # One or more settings still need a real value                                      # any missing?
        raise ValueError(  # Fail fast with an actionable message                                    # raise
            "Knowledge search settings still need a value: " + ", ".join(missing) + ". "  # Which names  # names
            f"Set them in the environment, or in {AGENT_ROOT / '.env'} (copy params.env to .env)."  # Where  # where
        )

    return ServiceNowCredentials.model_validate(values)  # Validate and return the connection details  # validate


def _apply_env_overrides(section_data: dict) -> dict:  # Overlay selected env vars onto the parsed section
    """Overlay a small allowlist of environment variables onto this agent's config data.

    What this function is:
        - The controlled override step: only the keys in _ENV_OVERRIDES are honoured, and only when
          the env var is actually set.

    Why this exists:
        - It lets an operator repoint the agent name or pin a version via Application Settings
          without redeploying the YAML file, while keeping the surface tiny and auditable.

    Security and production notes:
        1. The allowlist carries the agent name and version only, keeping the override surface
           small and auditable.
        2. Unset env vars are skipped so the YAML value stays in place; a malformed (non-dict)
           section is left untouched rather than being corrupted.

    Args:
        section_data: The parsed configuration dictionary for this agent.

    Returns:
        The same dictionary with any environment overrides applied.

    Example:
        >>> _apply_env_overrides({"foundry": {}})  # doctest: +SKIP
        {'foundry': {...}}
    """
    for env_var_name, (section_name, field_name) in _ENV_OVERRIDES.items():  # Iterate each supported override  # loop overrides
        env_value = os.environ.get(env_var_name)  # Read the environment variable (None when unset)  # read env var
        if env_value is None:  # Skip overrides whose env var is not set                             # unset skip
            continue  # Leave the YAML value in place                                                # keep yaml value
        section = section_data.setdefault(section_name, {})  # Ensure the target section dict exists  # ensure section
        if not isinstance(section, dict):  # Guard against a malformed (non-dict) section in the YAML  # dict guard
            continue  # Skip the override rather than corrupting the config                          # safe skip
        section[field_name] = env_value  # Set the overriding string value                           # set value
    return section_data  # Return the (possibly) modified configuration dictionary                   # return config
