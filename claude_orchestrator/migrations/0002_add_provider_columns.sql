ALTER TABLE jobs ADD COLUMN provider TEXT NOT NULL DEFAULT 'anthropic';

ALTER TABLE job_runs ADD COLUMN provider TEXT NOT NULL DEFAULT 'anthropic';

CREATE INDEX IF NOT EXISTS idx_jobs_provider_status
ON jobs(provider, status, priority DESC, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_job_runs_provider_job_id
ON job_runs(provider, job_id, started_at ASC);
