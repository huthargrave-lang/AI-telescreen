CREATE TABLE IF NOT EXISTS workspace_integrations (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    workspace_path TEXT NOT NULL,
    repo_path TEXT,
    discovered_at TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'workspace_scan',
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_workspace_integrations_job_id
ON workspace_integrations(job_id, discovered_at ASC);
