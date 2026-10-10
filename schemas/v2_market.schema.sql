-- Structure only. No production data, INSERTs or credentials.
CREATE TABLE adj_factor (
    ts_code    TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    adj_factor REAL NOT NULL,
    PRIMARY KEY (ts_code, trade_date)
);

CREATE TABLE cb_basic_snapshot (
  ts_code TEXT PRIMARY KEY, stk_code TEXT, bond_short_name TEXT,
  list_date TEXT, delist_date TEXT, maturity_date TEXT,
  conv_price REAL, remain_size REAL, issue_size REAL);

CREATE TABLE cb_daily_raw (
  ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
  close REAL, vol REAL, amount REAL,
  PRIMARY KEY(ts_code, trade_date));

CREATE TABLE cb_indicator (
  ts_code TEXT, trade_date TEXT, close REAL, stk_close REAL,
  conv_value REAL, premium_rate REAL, dual_low REAL,
  PRIMARY KEY(ts_code, trade_date));

CREATE TABLE corporate_actions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    venue          TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    ex_date        TEXT NOT NULL,
    kind           TEXT NOT NULL,
    ratio          TEXT,
    cash_per_share TEXT,
    currency       TEXT NOT NULL DEFAULT 'CNY',
    note           TEXT NOT NULL DEFAULT '',
    UNIQUE(venue, symbol, ex_date, kind)
);

CREATE TABLE events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    type           TEXT NOT NULL,
    account        TEXT NOT NULL,
    ts_event       TEXT NOT NULL,
    ts_recv        TEXT NOT NULL,
    payload        TEXT NOT NULL
);

CREATE TABLE experiments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    name         TEXT NOT NULL,
    git_sha      TEXT NOT NULL,
    data_start   TEXT NOT NULL,
    data_end     TEXT NOT NULL,
    params_hash  TEXT NOT NULL,
    params_json  TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE frozen_config (
    key        TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    params_json TEXT NOT NULL,
    frozen_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE ledger_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE premium_daily (
    date         TEXT NOT NULL,
    venue        TEXT NOT NULL,
    token_symbol TEXT NOT NULL,
    underlying   TEXT NOT NULL,
    token_close  REAL NOT NULL,
    und_close    REAL NOT NULL,
    premium_pct  REAL NOT NULL,
    PRIMARY KEY (date, venue, token_symbol)
);

CREATE TABLE schema_migrations( version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);

CREATE TABLE stk_close_daily (
  ts_code TEXT, trade_date TEXT, close REAL,
  PRIMARY KEY(ts_code, trade_date));

CREATE TABLE stock_name_history (
    ts_code     TEXT NOT NULL,
    name        TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT,
    ann_date    TEXT,
    name_change TEXT,
    PRIMARY KEY (ts_code, start_date, name)
);

CREATE INDEX idx_cbind_ts ON cb_indicator(ts_code, trade_date);

CREATE INDEX idx_events_account_seq ON events(account, seq);

CREATE INDEX idx_events_type ON events(type);

CREATE INDEX idx_name_hist_ts
    ON stock_name_history(ts_code, start_date);

CREATE INDEX idx_native_adj_factor_date ON adj_factor(trade_date,ts_code);

CREATE INDEX idx_native_cb_daily_raw_date ON cb_daily_raw(trade_date,ts_code);

CREATE INDEX idx_native_cb_indicator_date ON cb_indicator(trade_date,ts_code);

CREATE INDEX idx_native_stk_close_daily_date ON stk_close_daily(trade_date,ts_code);

CREATE TRIGGER experiments_immutable
BEFORE UPDATE ON experiments BEGIN
    SELECT RAISE(ABORT, 'experiments are append-only');
END;

CREATE TRIGGER experiments_no_delete
BEFORE DELETE ON experiments BEGIN
    SELECT RAISE(ABORT, 'experiments are append-only');
END;
