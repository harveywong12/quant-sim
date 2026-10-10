-- Structure only. No production data, INSERTs or credentials.
CREATE TABLE account (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    symbol           TEXT NOT NULL,
    initial_capital  REAL NOT NULL,
    cash             REAL NOT NULL,
    shares           INTEGER NOT NULL DEFAULT 0,
    pending_signal   TEXT NOT NULL DEFAULT 'HOLD',
    pending_date     TEXT,
    last_update_date TEXT
, pending_weight REAL NOT NULL DEFAULT 0, sellable_shares INTEGER NOT NULL DEFAULT 0, sellable_date TEXT, td_shares INTEGER NOT NULL DEFAULT 0, yd_shares INTEGER NOT NULL DEFAULT 0);

CREATE TABLE ai_issue_tracker (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    raised_date  TEXT NOT NULL,         -- 首次提出日期 YYYYMMDD
    text         TEXT NOT NULL,         -- 问题/建议原文
    status       TEXT NOT NULL DEFAULT 'open',   -- open / closed
    occurrences  INTEGER NOT NULL DEFAULT 1,     -- 被重复提出的次数
    last_status  TEXT,                  -- 最近核对结论（仍存在/已闭环/已恶化）
    last_checked TEXT,                  -- 最近核对日期 YYYYMMDD
    closed_date  TEXT
);

CREATE TABLE ai_memory (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_date TEXT NOT NULL,         -- 蒸馏日期 YYYYMMDD
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    memory_json  TEXT NOT NULL          -- 长期记忆 JSON（todos/lessons/watch）
);

CREATE TABLE ai_reports (
    date        TEXT PRIMARY KEY,       -- YYYYMMDD
    report_json TEXT NOT NULL,          -- AI 复盘 JSON
    model       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE ai_strategy_proposals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT NOT NULL,
    status              TEXT NOT NULL,
    model               TEXT,
    strategy_name       TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    data_cutoff         TEXT NOT NULL,
    data_revision       INTEGER NOT NULL,
    research_data_hash  TEXT NOT NULL,
    source_hash         TEXT NOT NULL,
    base_params_json    TEXT NOT NULL,
    account_cfg_json    TEXT NOT NULL,
    research_settings_json TEXT NOT NULL,
    hypothesis          TEXT NOT NULL,
    rationale           TEXT,
    market_regime       TEXT,
    parameter_sets_json TEXT NOT NULL,
    evaluation_plan     TEXT,
    prompt_hash         TEXT NOT NULL,
    raw_response_json   TEXT NOT NULL
);

CREATE TABLE alpha_accounts (
    strategy_name      TEXT PRIMARY KEY,
    initial_capital    REAL NOT NULL,
    cash               REAL NOT NULL,
    last_rebalance_date TEXT,
    last_mark_date      TEXT
, engine_version TEXT NOT NULL DEFAULT 'v1');

CREATE TABLE alpha_factor_cache (
    strategy_name TEXT PRIMARY KEY,
    date          TEXT NOT NULL,
    factors_json  TEXT NOT NULL
);

CREATE TABLE alpha_factor_history (
    strategy_name TEXT NOT NULL,
    date          TEXT NOT NULL,
    factors_json  TEXT NOT NULL,
    settled       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (strategy_name, date)
);

CREATE TABLE alpha_ic_weights (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    factor_name  TEXT NOT NULL,
    ic           REAL NOT NULL,
    date         TEXT NOT NULL
);

CREATE TABLE alpha_pending (
    strategy_name TEXT PRIMARY KEY,
    targets       TEXT NOT NULL,
    date          TEXT NOT NULL
);

CREATE TABLE alpha_positions (
    strategy_name TEXT NOT NULL,
    ts_code       TEXT NOT NULL,
    shares        INTEGER NOT NULL,
    avg_cost      REAL NOT NULL,
    PRIMARY KEY (strategy_name, ts_code)
);

CREATE TABLE alpha_snapshots (
    strategy_name TEXT NOT NULL,
    date          TEXT NOT NULL,
    total_value   REAL NOT NULL,
    PRIMARY KEY (strategy_name, date)
);

CREATE TABLE alpha_snapshots_by_version (
    strategy_name  TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    date           TEXT NOT NULL,
    total_value    REAL NOT NULL,
    PRIMARY KEY (strategy_name, engine_version, date)
);

CREATE TABLE alpha_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    date          TEXT NOT NULL,
    ts_code       TEXT NOT NULL,
    side          TEXT NOT NULL,
    price         REAL NOT NULL,
    shares        INTEGER NOT NULL,
    amount        REAL NOT NULL,
    fee           REAL NOT NULL,
    created_at    TEXT
, reason TEXT, engine_version TEXT NOT NULL DEFAULT 'v1');

CREATE TABLE backtest_data_state (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE bars (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   REAL NOT NULL,
    high   REAL NOT NULL,
    low    REAL NOT NULL,
    close  REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE challenger_accounts (
    proposal_id INTEGER NOT NULL,
    variant TEXT NOT NULL CHECK (variant IN ('baseline','challenger')),
    status TEXT NOT NULL DEFAULT 'active',
    strategy_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    params_json TEXT NOT NULL,
    account_cfg_json TEXT NOT NULL,
    initial_capital REAL NOT NULL,
    cash REAL NOT NULL,
    shares INTEGER NOT NULL DEFAULT 0,
    pending_signal TEXT NOT NULL DEFAULT 'HOLD',
    pending_date TEXT,
    pending_weight REAL NOT NULL DEFAULT 0,
    sellable_date TEXT,
    yd_shares INTEGER NOT NULL DEFAULT 0,
    last_update_date TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (proposal_id, variant)
);

CREATE TABLE challenger_snapshots (
    proposal_id INTEGER NOT NULL,
    variant TEXT NOT NULL,
    date TEXT NOT NULL,
    total_value REAL NOT NULL,
    PRIMARY KEY (proposal_id, variant, date)
);

CREATE TABLE challenger_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL,
    variant TEXT NOT NULL,
    date TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    shares INTEGER NOT NULL,
    amount REAL NOT NULL,
    fee REAL NOT NULL,
    reason TEXT
);

CREATE TABLE daily_run_log (
    date     TEXT PRIMARY KEY,
    success  INTEGER NOT NULL,
    message  TEXT,
    run_at   TEXT NOT NULL
);

CREATE TABLE engine_registry (
  name TEXT PRIMARY KEY,
  label TEXT,
  strategy TEXT,
  regime TEXT,
  symbol TEXT,
  symbols_json TEXT,
  params_json TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  note TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  retired_at TEXT
);

CREATE TABLE event_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,             -- YYYY-MM-DD HH:MM:SS
    level    TEXT NOT NULL,             -- INFO / WARNING / ERROR
    category TEXT NOT NULL,             -- intraday / daily / factor / alpha / race / system / datafeed
    message  TEXT NOT NULL
);

CREATE TABLE factor_account (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    initial_capital     REAL NOT NULL,
    cash                REAL NOT NULL,
    last_rebalance_date TEXT,
    last_mark_date      TEXT
, engine_version TEXT NOT NULL DEFAULT 'v1');

CREATE TABLE factor_pending (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    targets TEXT NOT NULL,
    date    TEXT NOT NULL
);

CREATE TABLE factor_snapshots (
    date        TEXT PRIMARY KEY,
    total_value REAL NOT NULL
);

CREATE TABLE factor_snapshots_by_version (
    engine_version TEXT NOT NULL,
    date           TEXT NOT NULL,
    total_value    REAL NOT NULL,
    PRIMARY KEY (engine_version, date)
);

CREATE TABLE factor_trades (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    date     TEXT NOT NULL,
    ts_code  TEXT NOT NULL,
    side     TEXT NOT NULL,
    price    REAL NOT NULL,
    shares   INTEGER NOT NULL,
    amount   REAL NOT NULL,
    fee      REAL NOT NULL
, created_at TEXT, reason TEXT, engine_version TEXT NOT NULL DEFAULT 'v1');

CREATE TABLE intraday_price (
    code       TEXT NOT NULL,           -- 6 位代码（512100 / 002384）
    ts         TEXT NOT NULL,           -- 5 分钟桶 YYYY-MM-DD HH:MM
    price      REAL,
    pre_close  REAL,
    open       REAL,
    high       REAL,
    low        REAL,
    volume     REAL,
    name       TEXT,
    PRIMARY KEY (code, ts)
);

CREATE TABLE intraday_runs (
    bucket      TEXT PRIMARY KEY,       -- YYYY-MM-DD HH:MM，向下取整到 5 分钟
    quote_date  TEXT NOT NULL,
    status      TEXT NOT NULL,
    trade_count INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE meta_nav (
    mode     TEXT NOT NULL,
    date     TEXT NOT NULL,
    nav      REAL NOT NULL,
    holdings TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (mode, date)
);

CREATE TABLE meta_state (
    mode       TEXT PRIMARY KEY,      -- c / a / b
    state_json TEXT NOT NULL          -- 元账户状态（净值/持仓/滞回计数）
);

CREATE TABLE pilot_directive (date TEXT PRIMARY KEY, payload TEXT);

CREATE TABLE process_checkpoints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,            -- 交易日 YYYYMMDD
    ts            TEXT NOT NULL,            -- 记录时刻 YYYY-MM-DD HH:MM:SS
    stage         TEXT NOT NULL,            -- preflight / fetch / settle / intraday / review / ...
    account       TEXT NOT NULL DEFAULT 'system',
    status        TEXT NOT NULL,            -- ok / warn / error / skipped / noop
    detail        TEXT,
    metrics_json  TEXT NOT NULL DEFAULT '{}',
    data_revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE qsim_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL UNIQUE,
    symbol TEXT,
    initial_capital REAL NOT NULL,
    cash REAL NOT NULL,
    shares INTEGER NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0.0,
    code_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_date TEXT NOT NULL
);

CREATE TABLE qsim_data_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    dataset TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    source_size INTEGER,
    source_mtime_ns INTEGER,
    source_sha256 TEXT,
    source_table_counts TEXT,
    backup_path TEXT,
    error TEXT, "source_table_watermarks" TEXT, "sync_action" TEXT,
    UNIQUE(run_id, dataset)
);

CREATE TABLE qsim_market_revisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, dataset TEXT NOT NULL,
 table_name TEXT NOT NULL, key_json TEXT NOT NULL, old_json TEXT NOT NULL,
 new_json TEXT NOT NULL, observed_at TEXT NOT NULL
);

CREATE TABLE qsim_native_ingest_runs (
 run_id TEXT PRIMARY KEY, session TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, status TEXT NOT NULL, result_json TEXT
);

CREATE TABLE qsim_order_intent_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    reason TEXT NOT NULL,
    prior_payload_json TEXT NOT NULL,
    revised_at TEXT NOT NULL
);

CREATE TABLE qsim_order_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    execution_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    allocation REAL NOT NULL,
    desired_count INTEGER NOT NULL,
    candidates_json TEXT NOT NULL,
    gate_json TEXT NOT NULL,
    source_market_as_of TEXT NOT NULL,
    model_hash TEXT NOT NULL,
    signal_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    executed_at TEXT,
    execution_json TEXT,
    last_error TEXT,
    UNIQUE(strategy, signal_date)
);

CREATE TABLE qsim_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES qsim_accounts(id),
    ts_code TEXT NOT NULL,
    shares INTEGER NOT NULL,
    avg_cost REAL NOT NULL DEFAULT 0.0,
    updated_date TEXT NOT NULL,
    UNIQUE(account_id, ts_code)
);

CREATE TABLE qsim_realtime (
    symbol TEXT PRIMARY KEY,
    price REAL NOT NULL,
    pre_close REAL NOT NULL DEFAULT 0.0,
    name TEXT DEFAULT '',
    updated_at TEXT NOT NULL
, open_price REAL NOT NULL DEFAULT 0.0, quote_date TEXT NOT NULL DEFAULT '', quote_time TEXT NOT NULL DEFAULT '');

CREATE TABLE qsim_realtime_nav (
    strategy TEXT NOT NULL,
    ts TEXT NOT NULL,
    total_value REAL NOT NULL,
    cash REAL NOT NULL,
    position_value REAL NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY(strategy, ts)
);

CREATE TABLE qsim_settlement_runs (
 run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, mode TEXT NOT NULL,
 metadata_json TEXT NOT NULL, appended_snapshots INTEGER NOT NULL,
 appended_trades INTEGER NOT NULL
);

CREATE TABLE qsim_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    strategy TEXT NOT NULL,
    total_value REAL NOT NULL,
    cash REAL NOT NULL,
    position_value REAL NOT NULL,
    strategy_version TEXT NOT NULL DEFAULT '',
    UNIQUE(date, strategy)
);

CREATE TABLE qsim_trade_provenance (trade_id INTEGER PRIMARY KEY REFERENCES qsim_trades(id), run_id TEXT NOT NULL);

CREATE TABLE qsim_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    shares INTEGER NOT NULL,
    amount REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0.0,
    reason TEXT DEFAULT '',
    code_hash TEXT NOT NULL DEFAULT ''
);

CREATE TABLE race_snapshots (
    strategy_name TEXT NOT NULL,
    date          TEXT NOT NULL,
    total_value   REAL NOT NULL,
    PRIMARY KEY (strategy_name, date)
);

CREATE TABLE race_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    date          TEXT NOT NULL,
    side          TEXT NOT NULL,       -- BUY / SELL
    price         REAL NOT NULL,
    shares        INTEGER NOT NULL,
    amount        REAL NOT NULL,
    fee           REAL NOT NULL,
    created_at    TEXT                 -- 模拟成交时刻（T+1 开盘 09:30）
, reason TEXT);

CREATE TABLE realtime_nav (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy   TEXT NOT NULL,          -- etf / factor / grid / ma_slope / keltner / rsi_boll
    ts         TEXT NOT NULL,          -- 盘中时刻 YYYY-MM-DD HH:MM:SS
    value      REAL NOT NULL           -- 实时净值（现金 + 持仓×实时价）
);

CREATE TABLE realtime_price (
    code       TEXT PRIMARY KEY,      -- 6 位代码（512100 / 002384）
    price      REAL NOT NULL,
    pre_close  REAL,
    name       TEXT,
    updated_at TEXT NOT NULL           -- YYYY-MM-DD HH:MM:SS
, open REAL, high REAL, low REAL, volume REAL);

CREATE TABLE research_holdout_reservations (
    holdout_key TEXT PRIMARY KEY,
    proposal_id INTEGER NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES ai_strategy_proposals(id)
);

CREATE TABLE research_registry (
  strategy TEXT PRIMARY KEY,
  symbol TEXT,
  schema_json TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE scheduler_heartbeat (
    name       TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    detail     TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE snapshots (
    date           TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    cash           REAL NOT NULL,
    position_value REAL NOT NULL,
    total_value    REAL NOT NULL,
    PRIMARY KEY (date, symbol)
);

CREATE TABLE stock_bars (
    ts_code TEXT NOT NULL,
    date    TEXT NOT NULL,
    open    REAL,
    high    REAL,
    low     REAL,
    close   REAL,
    vol     REAL, amount REAL, vwap REAL,
    PRIMARY KEY (ts_code, date)
);

CREATE TABLE stock_fundamental (
    ts_code             TEXT NOT NULL,
    ann_date            TEXT NOT NULL,
    end_date            TEXT NOT NULL,
    roe                 REAL,
    grossprofit_margin  REAL,
    netprofit_margin    REAL,
    eps                 REAL,
    debt_to_assets      REAL,
    PRIMARY KEY (ts_code, end_date)
);

CREATE TABLE stock_info (
    ts_code   TEXT PRIMARY KEY,
    name      TEXT,
    industry  TEXT,
    list_date TEXT
);

CREATE TABLE stock_positions (
    ts_code  TEXT PRIMARY KEY,
    shares   INTEGER NOT NULL,
    avg_cost REAL NOT NULL
);

CREATE TABLE stock_universe (
    ts_code    TEXT PRIMARY KEY,
    weight     REAL,
    trade_date TEXT
);

CREATE TABLE stock_universe_history (
    index_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    ts_code    TEXT NOT NULL,
    weight     REAL,
    PRIMARY KEY (index_code, trade_date, ts_code)
);

CREATE TABLE stock_valuation (
    ts_code       TEXT NOT NULL,
    date          TEXT NOT NULL,
    pe            REAL,
    pe_ttm        REAL,
    pb            REAL,
    dv_ratio      REAL,
    total_mv      REAL,
    circ_mv       REAL,
    turnover_rate REAL,
    PRIMARY KEY (ts_code, date)
);

CREATE TABLE strategy_accounts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name    TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    initial_capital  REAL NOT NULL,
    cash             REAL NOT NULL,
    shares           INTEGER NOT NULL DEFAULT 0,
    pending_signal   TEXT NOT NULL DEFAULT 'HOLD',
    pending_date     TEXT,
    pending_weight   REAL NOT NULL DEFAULT 0,
    last_update_date TEXT, sellable_shares INTEGER NOT NULL DEFAULT 0, sellable_date TEXT, td_shares INTEGER NOT NULL DEFAULT 0, yd_shares INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
    UNIQUE (strategy_name, symbol)
);

CREATE TABLE strategy_experiments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id     INTEGER NOT NULL UNIQUE,
    created_at      TEXT NOT NULL,
    data_revision   INTEGER NOT NULL,
    code_hash       TEXT NOT NULL,
    config_hash     TEXT NOT NULL,
    holdout_key     TEXT NOT NULL UNIQUE,
    data_hash       TEXT NOT NULL,
    bars_snapshot_json TEXT NOT NULL,
    result_json     TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES ai_strategy_proposals(id)
);

CREATE TABLE strategy_league (
    date        TEXT NOT NULL,          -- 评估日 YYYYMMDD
    strategy    TEXT NOT NULL,          -- etf_grid / race:keltner / factor / alpha:alpha191
    window_days INTEGER NOT NULL,
    ret         REAL,                   -- 窗口收益
    bench_ret   REAL,                   -- 窗口基准收益
    sharpe      REAL,
    max_dd      REAL,
    idle        INTEGER NOT NULL DEFAULT 0,  -- 窗口内无持仓且无成交
    status      TEXT NOT NULL,          -- active / benched
    action      TEXT NOT NULL DEFAULT 'none',-- none / relegated / promoted / advisory
    detail      TEXT,
    PRIMARY KEY (date, strategy)
);

CREATE TABLE strategy_version_history (
    strategy_kind  TEXT NOT NULL,
    strategy_name  TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    started_date   TEXT,
    ended_date     TEXT,
    initial_capital REAL NOT NULL,
    final_value    REAL,
    state_json     TEXT,
    archived_at    TEXT NOT NULL,
    PRIMARY KEY (strategy_kind, strategy_name, engine_version)
);

CREATE TABLE trades (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL,
    date      TEXT NOT NULL,
    side      TEXT NOT NULL,          -- BUY / SELL
    price     REAL NOT NULL,
    shares    INTEGER NOT NULL,
    amount    REAL NOT NULL,          -- 成交金额（含滑点）
    fee       REAL NOT NULL,
    cash_after REAL NOT NULL
, created_at TEXT, reason TEXT, strategy_name TEXT);

CREATE INDEX idx_ai_strategy_proposals_status
    ON ai_strategy_proposals (status, id DESC);

CREATE UNIQUE INDEX idx_alpha_ic_unique ON alpha_ic_weights (strategy_name, factor_name, date);

CREATE INDEX idx_event_log_ts ON event_log (ts DESC);

CREATE INDEX idx_iprice ON intraday_price (code, ts);

CREATE INDEX idx_process_checkpoints_date
    ON process_checkpoints (date, id);

CREATE INDEX idx_qsim_data_sync_runs_dataset
    ON qsim_data_sync_runs(dataset, id DESC);

CREATE INDEX idx_qsim_market_revision_run ON qsim_market_revisions(run_id);

CREATE INDEX idx_qsim_order_intent_revisions_intent
ON qsim_order_intent_revisions(intent_id);

CREATE INDEX idx_qsim_order_intents_execution
ON qsim_order_intents(execution_date, status);

CREATE INDEX idx_qsim_positions_acct ON qsim_positions(account_id);

CREATE INDEX idx_qsim_realtime_nav_strategy_ts
ON qsim_realtime_nav(strategy, ts);

CREATE INDEX idx_qsim_snapshots_date ON qsim_snapshots(date);

CREATE INDEX idx_qsim_trades_strategy ON qsim_trades(strategy);

CREATE UNIQUE INDEX idx_rtnav_uniq ON realtime_nav (strategy, ts);

CREATE INDEX idx_stock_bars_date ON stock_bars(date);

CREATE INDEX idx_universe_history_date
    ON stock_universe_history (index_code, trade_date);

CREATE TRIGGER trg_checkpoints_immutable_delete
BEFORE DELETE ON process_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'process_checkpoints is immutable');
END;

CREATE TRIGGER trg_checkpoints_immutable_update
BEFORE UPDATE ON process_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'process_checkpoints is immutable');
END;
