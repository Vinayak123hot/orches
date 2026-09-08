-- Run once against your Azure SQL database to create the tables the app uses.
-- Mirrors the old Cosmos container (one "conversations" + one "sessions" table,
-- replacing the single container that used a "type" field).
--
-- NOTE: keep these columns in sync with SQLITE_SCHEMA in app/db.py so
-- the test (SQLite) and prod (Azure SQL) schemas do not drift.
--
-- Every CREATE is guarded by IF OBJECT_ID(...) IS NULL, so this file is safe to re-run --
-- but for the same reason it will NOT apply a changed column to a table that already
-- exists. Drop the table (or ALTER it by hand) when the schema here changes.

IF OBJECT_ID('dbo.conversations', 'U') IS NULL
CREATE TABLE dbo.conversations (
    id                        NVARCHAR(200)  NOT NULL,
    user_id                   NVARCHAR(200)  NOT NULL,
    conversation_id           NVARCHAR(200)  NULL,
    stage                     NVARCHAR(50)   NULL,
    vars                      NVARCHAR(MAX)  NULL,   -- JSON string
    question                  NVARCHAR(MAX)  NULL,
    answer                    NVARCHAR(MAX)  NULL,
    session_id                NVARCHAR(200)  NULL,
    seq                       INT            NULL,
    title                     NVARCHAR(MAX)  NULL,
    -- Three timestamps, one job each (all ISO-8601 UTC strings):
    --   started_at       when the conversation began. Written once, never moves.
    --   last_updated_at  last activity of ANY kind -- every turn and every job poll bump
    --                    it. The reaper's staleness filter and its ordering both depend
    --                    on that, which is why it is separate from the other two.
    --   ended_at         when the stage reached DONE. Written once; NULL while the
    --                    conversation is open, so "still active" is an IS NULL test.
    started_at                NVARCHAR(50)   NULL,
    last_updated_at           NVARCHAR(50)   NULL,
    ended_at                  NVARCHAR(50)   NULL,
    -- ServiceNow references. Real columns rather than keys inside `vars`, so they can be
    -- indexed, joined and reported on. The flow still works with them through flow_vars:
    -- save() writes them out to these columns, load() merges them back in.
    interaction_id            NVARCHAR(200)  NULL,
    incident_id               NVARCHAR(200)  NULL,
    -- HOW the conversation ended. Not derivable from anything else: for an Outlook issue
    -- the incident is created UP FRONT, before the outcome is known, so
    -- "incident_id IS NOT NULL" is true for nearly every row and says nothing about the
    -- result.
    --   resolved   the user confirmed the issue was fixed
    --   escalated  we finished without fixing it; a human must pick up the ticket
    --   both 0     ended with no action needed (a greeting, or a non-IT question)
    -- DEFAULT 0 on both so a row is never NULL on either: a NULL would be neither true
    -- nor false and would drop out of both sides of every report.
    resolved                  BIT            NOT NULL
        CONSTRAINT DF_conversations_resolved DEFAULT 0,
    escalated                 BIT            NOT NULL
        CONSTRAINT DF_conversations_escalated DEFAULT 0,
    job_id                    NVARCHAR(200)  NULL,
    -- Three job fields, one purpose each:
    --   job_status   running | done | failed -- what the CODE branches on
    --   job_message  one USER-SAFE line for the current state -- shown in chat
    --   job_output   RAW stdout/stderr from the script on the user's device -- kept
    --                for support and NEVER returned to the browser. Device output
    --                routinely contains profile paths, mailbox addresses and internal
    --                host names, so that split must be preserved.
    job_status                NVARCHAR(50)   NULL,
    job_message               NVARCHAR(MAX)  NULL,
    job_output                NVARCHAR(MAX)  NULL,
    job_baseline              NVARCHAR(50)   NULL,   -- ISO-8601 UTC captured at trigger
    -- When the job was triggered. Expiry is measured from HERE, in wall-clock time --
    -- not in polls. A poll counter only advances when a browser is open, so an
    -- abandoned job never reached the old cap and stayed RUNNING forever.
    job_started_at            NVARCHAR(50)   NULL,
    CONSTRAINT PK_conversations PRIMARY KEY (user_id, id)
);
GO

-- Speeds up the session lookup in GET /conversations, which filters on
-- (user_id, session_id) -- neither of which the primary key can serve here,
-- since the PK leads with user_id + id.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_conversations_user_session'
      AND object_id = OBJECT_ID('dbo.conversations')
)
CREATE INDEX IX_conversations_user_session
    ON dbo.conversations (user_id, session_id);
GO

IF OBJECT_ID('dbo.sessions', 'U') IS NULL
CREATE TABLE dbo.sessions (
    id                        NVARCHAR(200)  NOT NULL,
    session_id                NVARCHAR(200)  NULL,
    user_id                   NVARCHAR(200)  NOT NULL,
    current_conversation_id   NVARCHAR(200)  NULL,
    title                     NVARCHAR(MAX)  NULL,
    -- started_at       set once, when the session is created
    -- last_updated_at  bumped on every turn; the sidebar list is ordered by it
    -- (no ended_at: a session has no terminal state of its own -- it drops off the
    --  sidebar when its CURRENT conversation reaches DONE)
    started_at                NVARCHAR(50)   NULL,
    last_updated_at           NVARCHAR(50)   NULL,
    CONSTRAINT PK_sessions PRIMARY KEY (user_id, id)
);
GO

-- FE-visible transcript: one row per message actually shown to the user
-- (Approach A). The /conversations API replays these rows in `seq` order
-- instead of reconstructing history from the agents' raw Foundry item stream.
IF OBJECT_ID('dbo.conversation_turns', 'U') IS NULL
CREATE TABLE dbo.conversation_turns (
    user_id                   NVARCHAR(200)  NOT NULL,
    conversation_id           NVARCHAR(200)  NOT NULL,
    seq                       INT            NOT NULL,   -- order shown to the user
    role                      NVARCHAR(20)   NOT NULL,   -- user | assistant
    content                   NVARCHAR(MAX)  NOT NULL,
    created_at                NVARCHAR(50)   NULL,
    CONSTRAINT PK_conversation_turns PRIMARY KEY (user_id, conversation_id, seq)
);
GO

-- One row per HTTP request WE serve: how long the caller actually waited, and how much
-- of that wait was downstream agents.
--
-- A table of its own because no existing one has the right grain. conversations is one
-- row per conversation (overwritten each turn, so only the last turn's timing would
-- survive). conversation_turns has no rows for a FAILED turn, and none at all for a
-- status poll. agent_calls has no row for a request that called no agent. All three drop
-- exactly the requests a latency chart exists to show -- the slow ones and the failed
-- ones -- so a p95 built from them looks healthy while users are complaining.
--
-- duration_ms - agent_ms is OUR overhead: flow logic, DB writes, translation. That
-- subtraction is the diagnostic question ("is the wait us, or them?"), which is why both
-- live on one row rather than needing a join to agent_calls.
--
-- VOLUME: /jobs/status is polled every ~30s by every active user, so at 100 concurrent
-- users this table takes ~288k rows/day (~10 GB/year at ~100 bytes a row). It needs a
-- retention job; 30 days keeps it near 8 GB and still covers any dashboard window.
IF OBJECT_ID('dbo.api_requests', 'U') IS NULL
CREATE TABLE dbo.api_requests (
    id                        BIGINT IDENTITY(1,1) NOT NULL,
    user_id                   NVARCHAR(200)  NOT NULL,
    conversation_id           NVARCHAR(200)  NULL,
    -- chat | chat_continue | jobs_status | sessions | transcript
    endpoint                  NVARCHAR(50)   NOT NULL,
    duration_ms               INT            NOT NULL,   -- total, what the caller waited
    agent_ms                  INT            NULL,       -- of which, downstream agents
    http_status               INT            NULL,
    is_error                  BIT            NOT NULL
        CONSTRAINT DF_api_requests_is_error DEFAULT 0,
    started_at                NVARCHAR(50)   NULL,
    CONSTRAINT PK_api_requests PRIMARY KEY (id)
);
GO

-- "p95 per endpoint over the last 7 days" -- the dashboard query.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_api_requests_endpoint_time'
      AND object_id = OBJECT_ID('dbo.api_requests')
)
CREATE INDEX IX_api_requests_endpoint_time
    ON dbo.api_requests (endpoint, started_at);
GO

-- Internal trace of every call we make to an agent Function App: what we sent, what
-- came back, and how long that app took. NOT user-visible -- conversation_turns is the
-- FE transcript; this is for debugging and per-agent latency reporting.
--
-- A child table rather than columns on conversations, because one conversation makes
-- MANY calls: the orchestrator runs once per greeting-loop turn, classify_1 repeats
-- until chat_close, classify_2 repeats through its ask_user loop, and a job adds a
-- start plus one row per status poll. There is no fixed number of columns that fits.
--
-- The follow-up question and the user's answer land in DIFFERENT rows on purpose. The
-- question is inside response_json when the agent asks it; the answer arrives in the
-- NEXT HTTP request, so it is inside that row's request_text. Ordering by id within a
-- conversation puts them next to each other.
IF OBJECT_ID('dbo.agent_calls', 'U') IS NULL
CREATE TABLE dbo.agent_calls (
    id                        BIGINT IDENTITY(1,1) NOT NULL,
    user_id                   NVARCHAR(200)  NOT NULL,
    conversation_id           NVARCHAR(200)  NOT NULL,
    -- orchestrator | classify_1 | classify_2 | diagnostics | troubleshoot | servicenow
    agent                     NVARCHAR(40)   NOT NULL,
    request_text              NVARCHAR(MAX)  NULL,   -- the body we posted
    response_json             NVARCHAR(MAX)  NULL,   -- the raw body that came back
    -- Time spent in the FUNCTION APP only: measured with a monotonic clock around the
    -- HTTP call, so our own JSON parsing and DB work are not included.
    duration_ms               INT            NOT NULL,
    -- 0 = ok, 1 = failed. DEFAULT 0 so a row can never carry NULL here -- a NULL would
    -- be neither ok nor failed and would drop out of both sides of every report.
    is_error                  BIT            NOT NULL
        CONSTRAINT DF_agent_calls_is_error DEFAULT 0,
    error_text                NVARCHAR(MAX)  NULL,   -- NULL when is_error = 0
    created_at                NVARCHAR(50)   NULL,
    CONSTRAINT PK_agent_calls PRIMARY KEY (id)
);
GO

-- "Everything that happened in this conversation" -- the debugging query.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_agent_calls_conversation'
      AND object_id = OBJECT_ID('dbo.agent_calls')
)
CREATE INDEX IX_agent_calls_conversation
    ON dbo.agent_calls (user_id, conversation_id, id);
GO

-- "How slow is classify_1 this week" -- the reporting query.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_agent_calls_agent_time'
      AND object_id = OBJECT_ID('dbo.agent_calls')
)
CREATE INDEX IX_agent_calls_agent_time
    ON dbo.agent_calls (agent, created_at);
GO
