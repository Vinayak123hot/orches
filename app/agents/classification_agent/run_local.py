####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# LOCAL test runner (NOT part of the served application). Drives the SAME entry point the support  #
# flow calls, so the agent can be exercised from a terminal without starting the web application.  #
#   1. Bootstraps the import path and environment, then builds the application's Foundry client.   #
#   2. Creates a Foundry conversation, exactly as the chat service does on a first turn.           #
#   3. Loops on a 'you>' prompt, calling classify(conversation_id, message) for each line typed.   #
#   4. Prints the agent's reply plus the outcome fields, and starts a fresh conversation on close. #
#                                                                                                  #
# USAGE (from a terminal):                                                                         #
#   az login                                        # so the credential chain has a token          #
#   pip install -r requirements.txt                 # once, into your virtual environment          #
#   python app/agents/classification_agent/run_local.py            # interactive chat              #
#   python app/agents/classification_agent/run_local.py "outlook stuck in outbox"   # one turn     #
#   python app/agents/classification_agent/run_local.py --json "outlook crashes"    # raw result   #
#                                                                                                  #
# PREREQS:                                                                                         #
#   1. AZURE_FOUNDRY_PROJECT_ENDPOINT set, in the environment or in a .env file at the repo root.  #
#   2. app/config.yaml filled in with the Foundry agent name.                                      #
#   3. .env in this folder, filled in from params.env (the knowledge search endpoint).             #
#   4. A signed-in credential: `az login`, or AZURE_SP_* / AZURE_* variables in the environment.   #
#                                                                                                  #
# This module is inert on import: everything runs under main(), so it can never affect the served  #
# application even though it sits beside the agent's own code.                                     #
#                                                                                                  #
# Source:-                                                                                         #
#   - app.core.config supplies the parsed application settings (endpoint, per-call timeout).       #
#   - app.clients.foundry supplies FoundryClient, the same connection the application builds.      #
#   - app.core.tracing supplies CallTrace, so each turn reports what it would record.              #
#   - app.core.errors supplies UpstreamTransient, raised when a turn cannot be completed.          #
#   - main (this folder) supplies FirstClassificationAgent, the agent's public entry point.        #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of type annotations (PEP 563)     # future import

import argparse  # Parse the optional message / --conv-id / --json command-line arguments           # stdlib argparse
import json  # Pretty-print the raw result dictionary when --json is requested                      # stdlib json
import os  # Read and normalise the credential environment variables                                # stdlib os
import sys  # Put the repository root on the import path and write friendly errors to stderr        # stdlib sys
import time  # Measure how long one turn takes end to end                                           # stdlib time
from pathlib import Path  # Resolve the repository root and the files the preflight checks look for  # stdlib pathlib

# ------------------------------- Import-path and environment bootstrap ---------------------------
# This file lives at app/agents/classification_agent/, so the repository root is four levels up.
# It goes on sys.path FIRST so `import app...` resolves the same way it does under the web server,
# whatever directory this script was launched from.
_REPO_ROOT = Path(__file__).resolve().parents[3]  # Repository root holding the app package         # repo root
if str(_REPO_ROOT) not in sys.path:  # Only insert it once, even if this module is re-imported       # already there?
    sys.path.insert(0, str(_REPO_ROOT))  # Make the app package importable regardless of the cwd     # path bootstrap

# Credential variable names differ between tooling: some environments carry AZURE_SP_* names while
# the credential chain reads the AZURE_* ones. Copy them across when only the former are present so
# a service principal already configured for this repository keeps working without being re-entered.
_CREDENTIAL_ALIASES = {  # Map of the name the credential chain reads to the name that may be set    # alias table
    "AZURE_TENANT_ID": "AZURE_SP_TENANT_ID",  # Directory (tenant) id                               # tenant
    "AZURE_CLIENT_ID": "AZURE_SP_CLIENT_ID",  # Application (client) id                              # client
    "AZURE_CLIENT_SECRET": "AZURE_SP_CLIENT_SECRET",  # Client secret                                # secret
}


def _bootstrap_environment() -> None:  # Load the .env file and normalise credential variable names
    """Load the repository's .env file and copy any aliased credential variables into place.

    What this function is:
        - The environment bootstrap. It must run BEFORE the application settings are imported,
          because those are parsed once at import time.

    Why this exists:
        - So the runner reads exactly the same configuration the web application does, and so a
          service principal stored under the AZURE_SP_* names is usable without re-entering it.

    Security and production notes:
        1. Nothing is printed or logged from the environment - only presence is ever checked.
        2. Values are copied only when the target name is absent, so an explicitly set variable
           always wins over an alias.

    Args:
        None.

    Returns:
        None.

    Example:
        >>> _bootstrap_environment()  # doctest: +SKIP
    """
    try:  # Load the repository-root .env when python-dotenv is available                           # try dotenv
        from dotenv import load_dotenv  # Imported here so a missing package is not fatal            # lazy import

        load_dotenv(_REPO_ROOT / ".env")  # Read the .env beside the application, if there is one    # load env
    except ImportError:  # python-dotenv is not installed; the real environment is used as-is        # no dotenv
        pass  # Continue with whatever the shell already provides                                    # carry on

    for target_name, alias_name in _CREDENTIAL_ALIASES.items():  # Consider each aliased variable    # each alias
        if not os.environ.get(target_name) and os.environ.get(alias_name):  # Only the alias is set  # alias only?
            os.environ[target_name] = os.environ[alias_name]  # Copy it under the expected name      # copy value


# ========================================== Preflight =============================================
def _preflight() -> "tuple[object, object]":  # Verify the local prerequisites, then load settings
    """Check the files and settings this runner needs, printing an actionable hint on failure.

    What this function is:
        - The fail-fast gate. It reports the missing piece in plain language instead of letting a
          traceback surface from somewhere deep in the agent.

    Why this exists:
        - Every failure here has a specific, well-known fix. Naming the fix is the difference
          between a two-minute start and an afternoon of guessing.

    Args:
        None.

    Returns:
        A tuple of (settings, agent_settings) once every check has passed.

    Raises:
        SystemExit: If any prerequisite is missing.

    Example:
        >>> settings, agent_settings = _preflight()  # doctest: +SKIP
    """
    config_path = _REPO_ROOT / "app" / "config.yaml"  # The application configuration file            # config path
    if not config_path.is_file():  # The agent's settings live in this file                          # missing config?
        _fail(  # Print a friendly, actionable message and stop                                      # fail out
            "app/config.yaml is missing.",
            f"Expected: {config_path}",
        )

    from app.core.config import settings  # Application settings, parsed once at import              # app settings

    if not settings.AZURE_FOUNDRY_PROJECT_ENDPOINT:  # No endpoint means there is nothing to connect to  # endpoint?
        _fail(  # Print a friendly, actionable message and stop                                      # fail out
            "AZURE_FOUNDRY_PROJECT_ENDPOINT is not set.",
            f"Set it in the environment, or in a .env file at {_REPO_ROOT}",
            "Example: AZURE_FOUNDRY_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>",
        )

    # Imported by its full path, not relatively: this file is executed as a script, so it has no
    # parent package of its own and a relative import would not resolve.
    from app.agents.classification_agent.runtime_config import load_settings  # Validated agent config  # agent config

    try:  # Load and validate the agent's section of the application configuration                   # try load
        agent_settings = load_settings()  # Parse app/config.yaml and check every value              # load config
    except Exception as config_error:  # A malformed or missing section stops the runner here        # bad config
        _fail(  # Print a friendly, actionable message and stop                                      # fail out
            "app/config.yaml could not be loaded.",
            f"{type(config_error).__name__}: {config_error}",
        )

    if not agent_settings.foundry.agent_name:  # Without an agent name there is nothing to invoke     # agent name?
        _fail(  # Print a friendly, actionable message and stop                                      # fail out
            "No Foundry agent name is configured.",
            "Set classification_agent.foundry.agent_name in app/config.yaml,",
            "or set the CLASSIFICATION_AGENT_NAME environment variable.",
        )

    from app.agents.classification_agent.runtime_config import load_servicenow_credentials  # Search settings  # credentials

    try:  # Confirm the search endpoint's connection details are usable before any turn runs         # try load
        load_servicenow_credentials()  # Read and validate them                                      # load
    except ValueError as credentials_error:  # One or more still need a value                        # missing?
        _fail(  # Print the message, which names the settings but never their values                 # fail out
            "The knowledge search is not configured.",
            str(credentials_error),
        )

    return settings, agent_settings  # Hand both settings objects back to the caller                  # return both


def _fail(*lines: str) -> "None":  # Print an actionable error block and stop with a non-zero code
    """Print an error block to stderr and exit.

    Args:
        *lines: The message lines, most important first.

    Returns:
        None. This function does not return.

    Raises:
        SystemExit: Always, with code 2.

    Example:
        >>> _fail("Something is missing.")  # doctest: +SKIP
    """
    print("\nERROR: " + lines[0], file=sys.stderr)  # Lead with what is wrong                        # headline
    for line in lines[1:]:  # Print each supporting detail indented under the headline               # each detail
        print("       " + line, file=sys.stderr)  # Indent so the block reads as one message         # detail line
    print("", file=sys.stderr)  # Trailing blank line so the block is easy to spot                   # spacer
    raise SystemExit(2)  # Stop with a non-zero exit code                                            # exit


# ========================================== One turn ==============================================
def _run_turn(agent_factory, conversation_id: str, message: str, as_json: bool) -> "dict | None":
    """Send one message through the agent's entry point and print the reply.

    What this function is:
        - One turn of the conversation: it builds a fresh agent (as the application does per
          request), calls classify(), and renders whatever came back.

    Why a fresh agent per turn:
        - The served application constructs the agent per request so each one carries its own call
          trace. Doing the same here keeps the runner faithful and reports each turn's own timing.

    Args:
        agent_factory: A callable returning a (agent, trace) pair for this turn.
        conversation_id: The Foundry conversation carrying this conversation's history.
        message: The user's message for this turn.
        as_json: True to print the raw result dictionary instead of the friendly rendering.

    Returns:
        The result dictionary, or None when the turn could not be completed.

    Example:
        >>> _run_turn(factory, "conv_abc", "outlook crashes", False)  # doctest: +SKIP
        {'chat_close': False, 'status': 'follow_up', ...}
    """
    from app.core.errors import UpstreamTransient  # Raised when a turn cannot be completed          # domain error

    agent, trace = agent_factory()  # Build this turn's agent and its call trace                     # build agent
    started = time.perf_counter()  # Start the stopwatch for this turn                               # start clock
    try:  # Run the turn through the agent's public entry point                                      # run turn
        result = agent.classify(conversation_id, message)  # THE call the support flow makes         # classify
    except UpstreamTransient as turn_error:  # The turn failed; the conversation itself is intact    # turn failed
        elapsed_ms = (time.perf_counter() - started) * 1000  # How long the failure took             # elapsed
        print(f"\n  [turn failed after {elapsed_ms:.0f} ms] {turn_error}")  # Report it plainly      # print failure
        print("  The conversation is still usable - send the message again.\n")  # What to do next   # print hint
        return None  # Nothing to return for a failed turn                                          # no result
    elapsed_ms = (time.perf_counter() - started) * 1000  # How long the successful turn took         # elapsed

    if as_json:  # Raw mode: print exactly what the support flow receives                            # json mode?
        print(json.dumps(result, indent=2, ensure_ascii=False))  # Pretty-print the result dict      # print json
    else:  # Friendly mode: show the reply, then a compact line of the routing fields                # friendly mode
        print(f"\nbot> {result.get('agent_message') or '(no message)'}")  # The line a user would see  # print reply
        print(  # A compact summary of what the flow would route on                                  # print meta
            f"     [{result.get('status')}"
            f" | chat_close={str(result.get('chat_close')).lower()}"
            f" | kb_id={result.get('kb_id') or '-'}"
            f" | {elapsed_ms:.0f} ms]"
        )
        if result.get("summary"):  # Only show the issue summary when the agent produced one         # have summary?
            print(f"     summary: {result['summary']}")  # The summary handed to whatever runs next  # print summary
        print("")  # Blank line before the next prompt                                               # spacer

    for row in trace.rows:  # Report what this turn would have written to the call-trace table       # each row
        if row["is_error"]:  # Only worth surfacing when the row records a failure                   # error row?
            print(f"     [trace] {row['agent']} error: {row['error_text']}")  # Show the cause       # print error
    return result  # Hand the result back so the caller can read chat_close                          # return result


# =========================================== Entry point ==========================================
def main() -> int:  # Interactive chat, or a single turn when a message is supplied
    """Run the classification agent locally against the real Foundry project.

    What this function is:
        - The runner itself: preflight, connect, create a conversation, then loop on a prompt.

    Why it mirrors the application:
        - It uses the application's own Foundry client and the agent's own entry point, so a
          problem reproduced here is a real problem, not an artefact of the harness.

    Args:
        None.

    Returns:
        A process exit code: 0 on a clean exit, 1 when the connection could not be established.

    Example:
        >>> main()  # doctest: +SKIP
        0
    """
    parser = argparse.ArgumentParser(  # Describe the command line this runner accepts               # build parser
        description="Chat with the Outlook Support Classification Agent locally."
    )
    parser.add_argument("message", nargs="?", default=None, help="A single message (omit for interactive chat).")  # msg
    parser.add_argument("--conv-id", default=None, help="Continue an existing Foundry conversation id.")  # conv id
    parser.add_argument("--json", action="store_true", help="Print the raw result dictionary for each turn.")  # json
    args = parser.parse_args()  # Parse the supplied arguments                                       # parse args

    settings, agent_settings = _preflight()  # Fail fast, with a hint, if anything is missing        # preflight

    # --- Imports deferred until after the environment bootstrap and the preflight checks ---
    from app.agents.classification_agent.main import FirstClassificationAgent  # The agent entry point  # agent class
    from app.clients.foundry import FoundryClient  # The application's own Foundry connection        # foundry client
    from app.core.tracing import CallTrace  # Per-turn call trace, as the application builds one     # call trace
    from app.event_hub import build_log_factory  # The application's structured-logging factory      # log factory

    print("")  # Blank line so the banner stands clear of the shell prompt                           # spacer
    print(f"  endpoint : {settings.AZURE_FOUNDRY_PROJECT_ENDPOINT}")  # Which project we are calling  # print endpoint
    print(f"  agent    : {agent_settings.foundry.agent_name}"  # Which agent will answer             # print agent
          f" (version: {agent_settings.foundry.agent_version or 'Foundry default'})")  # And which version
    print(f"  budget   : {settings.AGENT_HTTP_TIMEOUT}s per turn,"  # The wall-clock budget per turn  # print budget
          f" {agent_settings.foundry.max_search_rounds} knowledge-base fetches")  # And the search budget

    # --- Connect once, exactly as the application does at startup ---
    log_factory = build_log_factory(settings)  # One factory, exactly as the composition root builds it  # log factory
    foundry_client = FoundryClient(  # The same client class the application builds per worker       # build client
        settings.AZURE_FOUNDRY_PROJECT_ENDPOINT,  # Project endpoint from the environment            # endpoint
        timeout=settings.FOUNDRY_HTTP_TIMEOUT,  # Bounds the client's own short calls                # timeout
        max_retries=settings.FOUNDRY_MAX_RETRIES,  # Retry policy for those calls                    # retries
    )

    def build_agent():  # Construct this turn's agent and trace, as the composition root does        # agent factory
        """Return a fresh (agent, trace) pair for one turn."""
        trace = CallTrace()  # A fresh trace, so each turn reports only its own rows                 # new trace
        agent = FirstClassificationAgent(  # The agent, wired exactly as the application wires it    # build agent
            settings=settings,  # The application's parsed settings                                 # settings
            foundry=foundry_client,  # The shared, already-authenticated connection                  # foundry
            timeout=settings.AGENT_HTTP_TIMEOUT,  # Wall-clock budget for one whole turn             # timeout
            trace=trace,  # This turn's call trace                                                   # trace
            log_factory=log_factory,  # The shared structured-logging factory                        # log factory
        )
        return agent, trace  # Hand both back to the caller                                          # return pair

    def start_conversation() -> "str | None":  # Create a Foundry conversation and return its id     # conv factory
        """Create a new Foundry conversation, printing a hint if the connection fails."""
        try:  # The first call performs the sign-in and the connection                               # try connect
            conversation = foundry_client.create_conversation()  # Same call the chat service makes  # create conv
        except Exception as connect_error:  # Sign-in, network or endpoint problem                   # connect failed
            print(  # Point at the usual causes rather than printing a raw traceback                 # print hint
                f"\nERROR: could not reach the Foundry project: {connect_error}\n"
                "       Check: (1) `az login` has been run, or AZURE_SP_* / AZURE_* are set;\n"
                "              (2) AZURE_FOUNDRY_PROJECT_ENDPOINT is correct;\n"
                "              (3) the signed-in identity has access to the project.\n",
                file=sys.stderr,
            )
            return None  # Signal the failure to the caller                                          # signal none
        return conversation.id  # The stable conversation id for this chat                           # return id

    try:  # Everything below runs against the live connection; always release it on the way out      # guarded run
        conversation_id = args.conv_id or start_conversation()  # Continue one, or start a new one   # get conv id
        if conversation_id is None:  # The connection could not be established                       # no conv?
            return 1  # Stop with a non-zero exit code                                               # exit 1
        print(f"  conv id  : {conversation_id}\n")  # Show the id so a chat can be resumed later     # print conv id

        # --- Single-shot: a message was supplied on the command line ---
        if args.message:  # Run exactly one turn and exit                                            # single-shot?
            _run_turn(build_agent, conversation_id, args.message, args.json)  # Send it              # run once
            return 0  # Clean exit                                                                   # exit 0

        # --- Interactive: keep asking until the user leaves ---
        print("  Type your Outlook issue. 'exit' to quit, 'new' to start a fresh conversation.\n")  # banner
        while True:  # Loop until the user exits                                                     # chat loop
            try:  # Read the next line the user types                                                # read input
                user_message = input("you> ").strip()  # Prompt for the next message                 # prompt
            except (EOFError, KeyboardInterrupt):  # Ctrl-D / Ctrl-C leaves cleanly                  # ctrl-c/d
                print("")  # Newline so the shell prompt lands tidily                                # newline
                break  # Leave the loop                                                              # break
            if not user_message:  # Ignore blank lines                                               # empty?
                continue  # Ask again                                                                # continue
            if user_message.lower() in ("exit", "quit"):  # Explicit exit                            # exit word?
                break  # Leave the loop                                                              # break
            if user_message.lower() == "new":  # Start a fresh conversation on request               # new word?
                conversation_id = start_conversation()  # Create another conversation                # new conv
                if conversation_id is None:  # The connection failed on the way                      # no conv?
                    return 1  # Stop with a non-zero exit code                                       # exit 1
                print(f"\n  new conv id: {conversation_id}\n")  # Show the new id                    # print id
                continue  # Back to the prompt                                                       # continue

            result = _run_turn(build_agent, conversation_id, user_message, args.json)  # Send it     # run turn
            if result and result.get("chat_close"):  # The agent finished this conversation          # closed?
                print("  [conversation closed - starting a fresh one for your next message]\n")  # note it  # note
                conversation_id = start_conversation()  # A closed conversation cannot be continued  # new conv
                if conversation_id is None:  # The connection failed on the way                      # no conv?
                    return 1  # Stop with a non-zero exit code                                       # exit 1
                print(f"  new conv id: {conversation_id}\n")  # Show the new id                      # print id
        return 0  # Clean exit                                                                       # exit 0
    finally:  # Release the connection whichever way the runner leaves                               # cleanup
        foundry_client.close()  # Close the client's transport, as the application does at shutdown  # close client


if __name__ == "__main__":  # Allow `python app/agents/classification_agent/run_local.py`            # guard
    _bootstrap_environment()  # Load .env and normalise credential names BEFORE the app is imported  # bootstrap
    raise SystemExit(main())  # Run the chat and exit with its return code                           # run
