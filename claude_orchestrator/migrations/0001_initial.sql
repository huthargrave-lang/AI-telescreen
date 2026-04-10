CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    backend TEXT NOT NULL,
    task_type TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    prompt TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    idempotency_key TEXT,
    model TEXT,
    workspace_path TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency_key
ON jobs(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_jobs_status_priority
ON jobs(status, priority DESC, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_jobs_retry
ON jobs(status, next_retry_at);

CREATE TABLE IF NOT EXISTS job_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    backend TEXT NOT NULL,
    request_payload_json TEXT NOT NULL DEFAULT '{}',
    response_summary_json TEXT NOT NULL DEFAULT '{}',
    usage_json TEXT NOT NULL DEFAULT '{}',
    headers_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    exit_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_runs_job_id
ON job_runs(job_id, started_at ASC);

CREATE TABLE IF NOT EXISTS conversation_state (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    system_prompt TEXT,
    message_history_json TEXT NOT NULL DEFAULT '[]',
    compact_summary TEXT,
    tool_context_json TEXT NOT NULL DEFAULT '{}',
    last_checkpoint_at TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_artifacts_job_id
ON artifacts(job_id, created_at ASC);

CREATE TABLE IF NOT EXISTS scheduler_events (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_scheduler_events_job_id
ON scheduler_events(job_id, timestamp ASC);

CREATE TABLE IF NOT EXISTS message_batches (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    backend TEXT NOT NULL,
    upstream_batch_id TEXT NOT NULL,
    status TEXT NOT NULL,
    model TEXT,
    request_signature TEXT NOT NULL,
    custom_id_map_json TEXT NOT NULL DEFAULT '{}',
    request_payload_json TEXT NOT NULL DEFAULT '{}',
    response_payload_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    last_polled_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_message_batches_status
ON message_batches(status, updated_at ASC);
