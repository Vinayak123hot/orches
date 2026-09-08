#!/bin/bash
# App Service startup command. Configure it once, on the Web App:
#
#   az webapp config set -g <rg> -n <app> --startup-file "startup.sh"
#
# WHY EVERY FLAG IS HERE -- these are not defaults worth inheriting.
#
# --worker-class UvicornWorker
#     FastAPI is ASGI; gunicorn's default worker is WSGI and cannot serve it.
#
# --workers
#     Processes per instance. THIS MULTIPLIES THE SQL POOL: total Azure SQL sessions =
#     SQL_POOL_MAX_SIZE x workers x instances (45 x 2 x 2 = 180 by default). Check the
#     database tier's session limit before raising it. Rule of thumb is
#     (2 x cores) + 1, but the SQL ceiling binds first here, not the CPU.
#
# --timeout 600
#     gunicorn's default is 30s and it KILLS the worker at that point. A chat turn can
#     legitimately spend AGENT_HTTP_TIMEOUT (120s) on a single agent, so the default
#     would kill turns that were working fine. Note App Service's own front end still
#     cuts an idle connection at ~230s -- that is the real ceiling on a synchronous
#     request, and it is why device work runs as an async job with polling instead.
#
# --graceful-timeout 30
#     On a restart or scale-in, let an in-flight turn finish committing rather than
#     being killed mid-transaction. Matches the lifespan shutdown that cancels the
#     reaper (see app/main.py).
#
# --access-logfile '-' --error-logfile '-'
#     Log to stdout/stderr, which is where App Service log streaming and any
#     Application Insights agent pick them up. Without this the access log is silently
#     discarded.
#
# $PORT is set by the platform; 8000 is the default for Python containers and the
# fallback for running this script locally.
set -e

exec gunicorn app.main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers "${GUNICORN_WORKERS:-2}" \
  --bind "0.0.0.0:${PORT:-8000}" \
  --timeout 600 \
  --graceful-timeout 30 \
  --access-logfile '-' \
  --error-logfile '-'
