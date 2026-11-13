# -*- coding: utf-8 -*-
"""存储：watch 自己的库，绝不碰 amo 的任何文件。

规则（docs/ISOLATION-DESIGN.md 1.3）：
- 根目录写死 `data/plugin_data/Astrbot_amoupup_watch/`，**不提供配置项**；
- 所有路径走 `_path()`，resolve 后必须在根目录内，拒绝 `..` 与符号链接逃逸；
- 写前查磁盘余量，低于红线只读降级，宁可不写也不拖死 amo；
- 单写线程（由 Runtime.run_db 保证），WAL + busy_timeout，杜绝锁竞争。
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path

try:
    from astrbot.api import logger
except Exception:  # 离线单测
    logger = logging.getLogger("watch")

PLUGIN_NAME = "Astrbot_amoupup_watch"
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY, v TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL, kind TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, gen TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_day ON events(day, id);
CREATE TABLE IF NOT EXISTS people (
    pid TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '', weight REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, gen TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS edges (
    a TEXT NOT NULL, b TEXT NOT NULL, weight REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL, PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS settlements (
    day TEXT NOT NULL, kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
    PRIMARY KEY (day, kind)
);
CREATE TABLE IF NOT EXISTS positions (
    kind TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
    qty REAL NOT NULL DEFAULT 0, available REAL NOT NULL DEFAULT 0,
    cost_cents INTEGER NOT NULL DEFAULT 0, last_cents INTEGER NOT NULL DEFAULT 0,
    updated REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, code)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idem TEXT UNIQUE, ts REAL NOT NULL, side TEXT NOT NULL,
    kind TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
    qty REAL NOT NULL DEFAULT 0, price_cents INTEGER NOT NULL DEFAULT 0,
    amount_cents INTEGER NOT NULL DEFAULT 0, fee_cents INTEGER NOT NULL DEFAULT 0,
    pnl_cents INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts DESC);
CREATE TABLE IF NOT EXISTS loans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idem TEXT UNIQUE, kind TEXT NOT NULL, principal_cents INTEGER NOT NULL DEFAULT 0,
    balance_cents INTEGER NOT NULL DEFAULT 0, rate REAL NOT NULL DEFAULT 0,
    months INTEGER NOT NULL DEFAULT 0, monthly_cents INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
    value_cents INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending_credits (
    idem TEXT PRIMARY KEY, kind TEXT NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0, due_day TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS candidates (
    code TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '', expires_at REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS loan_installments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id INTEGER NOT NULL, seq INTEGER NOT NULL,
    due_day TEXT NOT NULL, principal_cents INTEGER NOT NULL DEFAULT 0,
    interest_cents INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
    paid_at REAL NOT NULL DEFAULT 0,
    UNIQUE(loan_id, seq)
);
CREATE TABLE IF NOT EXISTS market_cache (
    day TEXT NOT NULL, code TEXT NOT NULL, price_cents INTEGER NOT NULL DEFAULT 0,
    prev_cents INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, code)
);
"""


def default_root() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = Path(get_astrbot_data_path())
    except Exception:
        base = Path.cwd() / "data"
    return (base / "plugin_data" / PLUGIN_NAME).resolve()


class Store:
    def __init__(self, root=None, min_free_mb: int = 500):
        self.root = Path(root).resolve() if root else default_root()
        self.db_path = self._path("social.db")
        self.min_free_mb = int(min_free_mb)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self.readonly = False
        self.degraded_reason = ""

    # ---------------- 路径 / 磁盘 ----------------
    def _path(self, *parts) -> Path:
        target = (self.root.joinpath(*parts)).resolve()
        if target != self.root and self.root not in target.parents:
            raise ValueError("[WATCH] path escape: %s" % target)
        return target

    def free_mb(self) -> float:
        probe = self.root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            return shutil.disk_usage(str(probe)).free / (1024 * 1024)
        except Exception:
            return 0.0

    def check_space(self) -> bool:
        if self.min_free_mb <= 0:
            return True
        free = self.free_mb()
        if free < self.min_free_mb:
            if not self.readonly:
                logger.warning("[WATCH] degraded reason=disk_low free=%.0fMB min=%dMB",
                               free, self.min_free_mb)
            self.readonly = True
            self.degraded_reason = "disk_low"
            return False
        return True

    # ---------------- 生命周期（都在 DB 线程里调） ----------------
    def init(self) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        for ddl in (
            "ALTER TABLE loans ADD COLUMN status TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE positions ADD COLUMN ready_day TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version < SCHEMA_VERSION:
            conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES ('created_at', ?)", (str(time.time()),))
        conn.commit()
        with self._lock:
            self._conn = conn
        return {"root": str(self.root), "schema": SCHEMA_VERSION, "free_mb": round(self.free_mb(), 1)}

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    # ---------------- 基础写读 ----------------
    def _write(self, sql: str, params=()) -> int:
        if self._conn is None:
            raise RuntimeError("[WATCH] store not initialized")
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return int(cur.rowcount)

    def _query(self, sql: str, params=()) -> list:
        if self._conn is None:
            return []
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    # ---------------- 通用读写（给金融模块） ----------------
    def write(self, sql: str, params=()) -> int:
        return self._write(sql, params)

    def query(self, sql: str, params=()) -> list:
        return self._query(sql, params)

    # ---------------- 业务最小集 ----------------
    def add_event(self, day: str, kind: str, weight: float = 0.0, payload: str = "{}",
                  gen: str = "", max_per_day: int = 2000) -> bool:
        if self.readonly or not self.check_space():
            return False
        count = self._query("SELECT COUNT(*) FROM events WHERE day = ?", (day,))[0][0]
        if count >= max_per_day:
            # 触顶：丢当天权重最低的一条，再插入（保证新事件进得来）
            self._write(
                "DELETE FROM events WHERE id = (SELECT id FROM events WHERE day = ? "
                "ORDER BY weight ASC, id ASC LIMIT 1)", (day,))
        self._write(
            "INSERT INTO events(day, kind, weight, payload, created_at, gen) VALUES (?,?,?,?,?,?)",
            (day, kind, float(weight), payload, time.time(), gen))
        return True

    def settle(self, day: str, kind: str, payload: str = "{}") -> bool:
        """幂等结算：UNIQUE(day, kind)，重复调用返回 False。"""
        if self.readonly or not self.check_space():
            return False
        rows = self._write(
            "INSERT OR IGNORE INTO settlements(day, kind, payload, created_at) VALUES (?,?,?,?)",
            (day, kind, payload, time.time()))
        return rows > 0

    def trim_events(self, keep_days: int = 90, now: float | None = None) -> int:
        cutoff = (now or time.time()) - max(1, int(keep_days)) * 86400
        return self._write("DELETE FROM events WHERE created_at < ?", (cutoff,))

    def touch(self) -> None:
        self._write("INSERT INTO meta(k, v) VALUES ('last_beat', ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (str(time.time()),))

    def stats(self) -> dict:
        out = {"readonly": self.readonly, "reason": self.degraded_reason,
               "free_mb": round(self.free_mb(), 1), "db": str(self.db_path)}
        for table in ("events", "people", "edges", "settlements"):
            try:
                out[table] = self._query("SELECT COUNT(*) FROM %s" % table)[0][0]
            except Exception:
                out[table] = -1
        try:
            out["db_mb"] = round(self.db_path.stat().st_size / (1024 * 1024), 2)
        except Exception:
            out["db_mb"] = 0.0
        return out
