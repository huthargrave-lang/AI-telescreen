CREATE TABLE IF NOT EXISTS project_operator_feedback (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    severity TEXT,
    area TEXT,
    notes TEXT NOT NULL,
    screenshot_reference TEXT,
    requires_followup INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES saved_projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_project_operator_feedback_project_created
ON project_operator_feedback(project_id, created_at DESC);
