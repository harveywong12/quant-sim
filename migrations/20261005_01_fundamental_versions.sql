-- Additive audit migration. Apply ONLY to an authorized isolated quant.db backup.
-- Mirrors native_market.AUDIT_SCHEMA; old code ignores this table on rollback.
-- No historical availability is inferred or backfilled by this migration.
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS qsim_fundamental_versions (
 ts_code TEXT NOT NULL, end_date TEXT NOT NULL, ann_date TEXT NOT NULL,
 observed_at TEXT NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(ts_code,end_date,ann_date,payload_hash)
);
COMMIT;
