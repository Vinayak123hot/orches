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


# ======================================== Path resolution ========================================
def resolve_path(relative_or_absolute_path: str) -> Path:  # Resolve a data path against the agent folder
    """Resolve a path against this agent's folder (absolute paths pass through).

    What this function is:
        - The single path anchor for this agent's data files: it turns a relative config path into
          an absolute one under AGENT_ROOT, and returns already-absolute paths unchanged.

    Why this exists:
        - The knowledge-base index ships beside this agent's code, while the configuration naming
          it lives at the application root. Anchoring here keeps a relative path in that shared
          config file pointing at THIS agent's data rather than at the application root.

    Args:
        relative_or_absolute_path: A relative or absolute path string.

    Returns:
        An absolute Path.

    Example:
        >>> resolve_path("kb_index.json")  # doctest: +SKIP
        PosixPath('/home/site/wwwroot/app/agents/classification_agent/kb_index.json')
    """
    candidate_path = Path(relative_or_absolute_path)  # Wrap the input string as a Path object       # wrap path
    if candidate_path.is_absolute():  # If the path is already absolute                              # absolute check
        return candidate_path  # Return it unchanged                                                 # pass through
    return (AGENT_ROOT / candidate_path).resolve()  # Otherwise anchor it to this agent's folder     # anchor + resolve


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


class KnowledgeBaseConfig(BaseModel):  # Typed model for knowledge-base source settings
    """Knowledge-base source settings.

    What this model is:
        - The typed shape of the in-process knowledge-base settings: which index file to load as
          the candidate pool the agent selects from.

    Why this exists:
        - Knowledge-base selection runs in-process, so the loader only needs the index location;
          the path is resolved against this agent's folder by resolve_path.
    """

    index_path: str = "kb_index.json"  # Path to the KB index (relative paths anchor to this agent's folder)  # index location


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
