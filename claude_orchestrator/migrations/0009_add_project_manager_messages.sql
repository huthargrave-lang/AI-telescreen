CREATE TABLE IF NOT EXISTS project_manager_messages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES saved_projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_project_manager_messages_project_created
ON project_manager_messages(project_id, created_at DESC);
