ALTER TABLE jobs
ADD COLUMN project_id TEXT;

CREATE INDEX IF NOT EXISTS idx_jobs_project_id
ON jobs(project_id, created_at DESC);
