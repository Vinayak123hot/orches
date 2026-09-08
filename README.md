# IT Support Orchestrator API

FastAPI app on **Azure App Service** (Linux, Python). It drives the support conversation:
classify what the user asked, raise and update the ServiceNow ticket, and run diagnostics
or remediation on their machine as an asynchronous job.

The agents it coordinates are **in-process Python classes**, one folder each under
`app/agents/`. They used to be separate Function Apps called over HTTP.

## Layout

```
orchestrator-api/            <- the deployment root (requirements.txt must stay here)
├── startup.sh               App Service startup command (gunicorn + uvicorn worker)
├── requirements.txt         runtime deps         (Oryx installs these)
├── requirements-dev.txt     dev-only deps        (never deployed)
├── .env.example             copy to .env for local runs
├── .deployment              build on the server during deploy
├── app/
│   ├── main.py              the FastAPI app, lifespan, error -> status mapping
│   ├── deps.py              the composition root: everything is constructed here
│   ├── api/                 controllers, response shapes, request/response models
│   ├── services/            one class per use case: sequencing and transactions
│   ├── domain/              the stage machine, job dispatch, run-state meaning
│   ├── agents/              one folder per agent, called in-process
│   ├── db/                  connection factory + every SQL statement
│   ├── clients/             Azure AI Foundry SDK client
│   ├── workers/             background tasks (the job reaper)
│   └── core/                config, constants, errors, tracing
└── scripts/                 one-off local utilities, off the deployment path
```

Layers may only import downwards: `api -> services -> domain -> agents/db`, with `core`
at the bottom importing nothing. `app/__init__.py` spells out why.

## Run it locally

```bash
python -m venv .venv && .venv\Scripts\activate        # Windows
pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env                                 # then fill it in
uvicorn app.main:app --reload --port 8000
```

- Docs: `http://127.0.0.1:8000/docs`
- Health: `http://127.0.0.1:8000/health`

Endpoints (all under `API_PREFIX`, default `/api`):

| Method | Path |
|---|---|
| POST | `/api/chat` |
| POST | `/api/chat/continue` |
| GET | `/api/jobs/status?user_id=&conversation_id=&session_id=` |
| GET | `/api/sessions/{user_id}` |
| GET | `/api/conversations/{user_id}/{session_id}` |
| GET | `/health` (outside the prefix, for the platform probe) |

The frontend's `VITE_API_URL` must include the prefix — e.g. `https://<app>.azurewebsites.net/api`.

`SQLITE_DB_PATH=local.db` in `.env` runs against a local file DB, so no Azure SQL and no
driver are needed. Set `REAPER_ENABLED=false` if the background sweep is noise while
debugging.

## Deploy

```bash
az webapp up -g <rg> -n <app> --runtime "PYTHON:3.11"
az webapp config set -g <rg> -n <app> --startup-file "startup.sh"
az webapp config set -g <rg> -n <app> --always-on true        # REQUIRED, see below
az webapp config appsettings set -g <rg> -n <app> --settings @appsettings.json
```

Application Settings are read as plain environment variables, so `.env.example` is the
full list of what to set. Use a **managed identity** and leave `AZURE_SP_*` empty where
possible; it needs the Azure AI Foundry data-plane role, plus whatever each agent's own
backend requires.

### Two things that will bite

**Always On is not optional.** The job reaper is an asyncio task inside this app
(`app/workers/reaper.py`), not a platform timer. Without Always On the app idles out, no
sweeps run, and a job whose user closed their browser stays RUNNING forever with its
ticket saying only "diagnostics started". It also runs on every instance and in every
gunicorn worker — safe, because completing a job is a compare-and-swap, but it means N
workers do N reads.

**Worker count multiplies your SQL sessions.**

```
Azure SQL sessions = SQL_POOL_MAX_SIZE x GUNICORN_WORKERS x instances
                     45               x 2                x 2         = 180
```

Check the database tier's session limit before raising either number. See the notes in
`startup.sh` and `app/core/config.py`.

## Implementing an agent

Each agent is a class with documented method signatures and one hook to fill in:

```python
# app/agents/<name>/main.py
class OrchestratorAgent(Agent):
    label = "orchestrator"

    def classify(self, conversation_id: str, message: str) -> dict:
        return self._call(conversation_id=conversation_id, message=message)
```

Implement `_call(**payload)` — it receives what used to be the HTTP request body — and
return what that agent's Function App used to return; the contract is in the module's
docstring. Until then it raises `NotImplementedError` naming the fields it was handed.
Everything behind `_call` (Foundry, Graph/Intune, ServiceNow, KB search, prompts, retries)
belongs to the team that owns the agent and lives in that folder.

Wrap the outbound call in `self.traced({...})` so it lands in the `agent_calls` table —
an agent that skips it has no latency recorded anywhere.

## What moved here from the Functions app

`../legacy-functions/` holds what App Service does not use, kept rather than deleted:
`host.json`, `local.settings.json`, `http_app/`, `reaper/` (the timer trigger, now
`app/workers/reaper.py`), `dead-clients/` (the HTTP clients the in-process agents
replaced) and `pre-split/` (the original single-file `routes.py`, `services.py`,
`repositories.py`, `db.py`). Nothing in this app imports any of it.
