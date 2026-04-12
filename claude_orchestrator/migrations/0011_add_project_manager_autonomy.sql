ALTER TABLE saved_projects
ADD COLUMN autonomy_mode TEXT NOT NULL DEFAULT 'minimal';

ALTER TABLE project_manager_states
ADD COLUMN workflow_state TEXT NOT NULL DEFAULT 'idle';

ALTER TABLE project_manager_states
ADD COLUMN active_autonomy_session_id TEXT;

ALTER TABLE project_manager_states
ADD COLUMN active_job_id TEXT;

ALTER TABLE project_manager_states
ADD COLUMN auto_tasks_run_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE project_manager_states
ADD COLUMN project_guidance_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE project_manager_states
ADD COLUMN last_guidance_saved_at TEXT;
