-- Structure only. No production data, INSERTs or credentials.
CREATE TABLE experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT,
    git_sha TEXT,
    code_hash TEXT,
    data_fingerprint TEXT,
    payload TEXT NOT NULL
);

CREATE INDEX idx_experiments_kind_ts ON experiments(kind, ts);
