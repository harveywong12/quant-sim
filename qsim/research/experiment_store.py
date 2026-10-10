"""Append-only experiment store: JSONL + sqlite dual-write with provenance.

JSONL 是既有兼容格式（dashboard 直接读取），sqlite 增加可查询的溯源列：
git_sha / code_hash / data_fingerprint，使每条实验记录可追溯到
"哪版代码 + 哪版数据" 跑出该结果。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, timeout=5
        )
        return out.stdout.decode().strip() or None
    except Exception:
        return None


@lru_cache(maxsize=1)
def _code_hash() -> str | None:
    """qsim/ 源码内容哈希（相对路径+内容），用于区分构建版本。"""
    src = ROOT / "qsim"
    if not src.is_dir():
        return None
    h = hashlib.sha256()
    try:
        for p in sorted(src.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            h.update(str(p.relative_to(ROOT)).encode())
            h.update(p.read_bytes())
    except Exception:
        return None
    return h.hexdigest()[:16]


@lru_cache(maxsize=1)
def _data_fingerprint() -> str | None:
    """quant.db bars/stock_bars 的 MAX(date)+COUNT 轻量指纹。

    与影子对账的数据门同口径，避免引入重聚合的 data_version()。
    """
    db = ROOT / "data" / "quant.db"
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        parts = []
        for table in ("bars", "stock_bars"):
            row = conn.execute(f"SELECT MAX(date), COUNT(*) FROM {table}").fetchone()
            parts.append(f"{table}:{row[0]}:{row[1]}")
        conn.close()
        return "|".join(parts)
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT,
    git_sha TEXT,
    code_hash TEXT,
    data_fingerprint TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experiments_kind_ts ON experiments(kind, ts);
"""


class ExperimentStore:
    def __init__(self, path: str | Path, sqlite_path: str | Path | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sqlite_path = Path(sqlite_path) if sqlite_path else self.path.with_suffix(".db")
        self._init_sqlite()

    def _init_sqlite(self) -> None:
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.sqlite_path)
        try:
            conn.executescript(_SCHEMA)
            n = conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
            if n == 0 and self.path.is_file():
                # 一次性回填历史 jsonl 记录（溯源列为空）
                for entry in self.load():
                    conn.execute(
                        "INSERT INTO experiments(ts, kind, payload) VALUES (?, ?, ?)",
                        (entry.get("ts"), entry.get("kind"),
                         json.dumps(entry, ensure_ascii=False, sort_keys=True)),
                    )
            conn.commit()
        finally:
            conn.close()

    def record(self, entry: dict[str, Any]) -> dict[str, Any]:
        record = {
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            **entry,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        conn = sqlite3.connect(self.sqlite_path)
        try:
            conn.execute(
                "INSERT INTO experiments(ts, kind, git_sha, code_hash, data_fingerprint, payload)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (record.get("ts"), record.get("kind"), _git_sha(), _code_hash(),
                 _data_fingerprint(),
                 json.dumps(record, ensure_ascii=False, sort_keys=True)),
            )
            conn.commit()
        finally:
            conn.close()
        return record

    def load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def query(self, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """从 sqlite 查询（含溯源列），行格式：记录字段 + 顶层 provenance。"""
        conn = sqlite3.connect(self.sqlite_path)
        try:
            if kind:
                rows = conn.execute(
                    "SELECT ts, kind, git_sha, code_hash, data_fingerprint, payload"
                    " FROM experiments WHERE kind = ? ORDER BY id DESC LIMIT ?",
                    (kind, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT ts, kind, git_sha, code_hash, data_fingerprint, payload"
                    " FROM experiments ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        finally:
            conn.close()
        out = []
        for ts, k, sha, ch, fp, payload in rows:
            entry = json.loads(payload)
            entry["git_sha"] = sha
            entry["code_hash"] = ch
            entry["data_fingerprint"] = fp
            out.append(entry)
        return out
