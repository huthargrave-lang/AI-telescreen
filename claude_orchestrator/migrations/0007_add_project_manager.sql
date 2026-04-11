CREATE TABLE IF NOT EXISTS project_manager_states (
    project_id TEXT PRIMARY KEY,
    current_phase TEXT NOT NULL DEFAULT 'planning',
    summary TEXT NOT NULL DEFAULT '',
    stable_project_facts_json TEXT NOT NULL DEFAULT '{}',
    testing_status TEXT NOT NULL DEFAULT 'not_requested',
    needs_manual_testing INTEGER NOT NULL DEFAULT 0,
    rolling_summary TEXT,
    rolling_facts_json TEXT NOT NULL DEFAULT '{}',
    latest_recommendation_type TEXT,
    latest_recommendation_reason TEXT,
    latest_recommendation_json TEXT,
    last_job_ingested_at TEXT,
    last_compacted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES saved_projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS project_manager_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source_job_id TEXT,
    attempt_number INTEGER,
    outcome_status TEXT,
    event_type TEXT NOT NULL DEFAULT 'job_outcome',
    created_at TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(project_id) REFERENCES saved_projects(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_project_manager_events_job_attempt
ON project_manager_events(project_id, source_job_id, attempt_number, outcome_status, event_type);

CREATE INDEX IF NOT EXISTS idx_project_manager_events_project_created
ON project_manager_events(project_id, created_at DESC);
