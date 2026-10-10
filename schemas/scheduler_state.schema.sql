-- Structure only. No production data, INSERTs or credentials.
CREATE TABLE fail_counts (
    job_id TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);

CREATE TABLE job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL,
    scheduled_at TEXT,
    error TEXT,
    finished_at TEXT NOT NULL
);

CREATE INDEX idx_job_runs_job_id ON job_runs(job_id, id);
