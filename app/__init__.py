"""IT Support Orchestrator API.

An Azure App Service (Linux, Python) app: gunicorn runs `app.main:app` through
uvicorn's worker -- see startup.sh.

LAYERS, top to bottom. Each one may only import from the ones below it.

    main.py       builds the FastAPI app, owns the lifespan, maps domain errors -> status
    deps.py       the composition root: every object is constructed here and nowhere else
    api/          the HTTP edge -- controllers, response shapes, request/response models
    services/     one class per use case: sequencing, transactions, persistence
    domain/       the business rules: the stage machine, job dispatch, run-state meaning
    agents/       one folder per agent, called in-process as plain classes
    db/           the connection factory and every SQL statement
    clients/      third-party SDK clients (Azure AI Foundry)
    workers/      background tasks started by the lifespan (the job reaper)
    core/         config, constants, errors, tracing -- imported by all, imports none

WHY THE DIRECTION MATTERS. `domain/` knows nothing about HTTP, so the same stage machine
runs from a request, from the reaper's background sweep and from a test with no web
server. `db/` knows nothing about the flow, so a schema change stops at its own folder.
An import that points back up the list -- domain reaching into api, core reaching into
services -- is the signal that something is in the wrong place.

THE AGENTS ARE IN-PROCESS. Each one used to be its own Function App reached over HTTP;
they are now classes under agents/, constructed in deps.py and called with real
parameters. This app makes no outbound HTTP call of its own any more, and an agent
contract is a Python signature rather than a JSON body. See agents/__init__.py for who
owns what inside an agent folder.

Classes are used where an object holds state or collaborators that must outlive a single
call -- the lazily built Foundry client, a DB connection, injected dependencies. Route
handlers and the pure row/payload helpers stay functions, because a class whose only
field is unused adds ceremony without structure.
"""
