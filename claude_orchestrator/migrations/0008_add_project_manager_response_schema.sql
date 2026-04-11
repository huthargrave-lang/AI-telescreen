ALTER TABLE project_manager_states
ADD COLUMN latest_response_json TEXT;

ALTER TABLE project_manager_states
ADD COLUMN display_snapshot_json TEXT NOT NULL DEFAULT '{}';
