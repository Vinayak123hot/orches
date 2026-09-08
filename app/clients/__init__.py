"""Third-party SDK clients.

    foundry.py   FoundryClient -- the Azure AI Foundry AIProjectClient, lazy and
                 thread-safe

A class because it holds something that must outlive a single call: the built client. It
is instantiated once per worker process in deps.py and handed to the services and to
every agent, so the credential handshake happens once rather than per request.

WHAT USED TO BE HERE, and where its behaviour went. http.py (HttpClient and its pooled
requests.Session), agents.py (AgentClient), servicenow.py, multilingual.py and
jobs_agent.py all existed to POST to the sibling agent Function Apps. Those agents are
now classes under app/agents/, called directly, so the whole transport layer is gone --
with it the connection pooling, the ?code= function keys and the URL -> agent-name
mapping in core/tracing.py. The behaviour those modules protected did NOT go with them:

    the ServiceNow best-effort / must-raise split   -> agents/servicenow/main.py
    "translation must never break a conversation"   -> agents/multilingual/main.py
    the {"success": false} failure shape            -> agents/classification_second/
    message vs output, and the no-simulation rule   -> domain/run_state.py, domain/jobs.py

The originals are parked in ../../legacy-functions/dead-clients/ rather than deleted, so
the diff is recoverable while this is still settling. Nothing imports them.
"""
