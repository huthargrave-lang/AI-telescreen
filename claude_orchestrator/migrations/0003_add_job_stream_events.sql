CREATE TABLE IF NOT EXISTS job_stream_events (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    backend TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    phase TEXT,
    message TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_job_stream_events_job_id
ON job_stream_events(job_id, timestamp ASC);
