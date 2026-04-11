CREATE TABLE IF NOT EXISTS saved_projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    default_backend TEXT,
    default_provider TEXT,
    default_base_branch TEXT,
    default_use_git_worktree INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_saved_projects_name
ON saved_projects(name, updated_at DESC);
