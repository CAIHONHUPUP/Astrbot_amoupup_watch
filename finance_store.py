# -*- coding: utf-8 -*-
"""金融数据仓库：只管自己库里的持仓 / 成交 / 贷款 / 资产 / 行情缓存。

不碰 amo 的钱：余额永远走 amo.wallet，本模块只记「持仓与流水」。
"""
from __future__ import annotations

import time


class FinanceRepo:
    def __init__(self, store):
        self.store = store

    # ---------- 持仓 ----------
    def positions(self, kind=""):
        sql = ("SELECT kind, code, name, qty, available, cost_cents, last_cents, updated "
               "FROM positions")
        params = ()
        if kind:
            sql += " WHERE kind=?"
            params = (str(kind),)
        return [dict(row) if not isinstance(row, dict) else row
                for row in self._rows(sql + " ORDER BY kind, code", params)]

    def _rows(self, sql, params=()):
        rows = self.store.query(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in rows]

    def position(self, kind, code):
        rows = self._rows("SELECT * FROM positions WHERE kind=? AND code=?", (str(kind), str(code)))
        return rows[0] if rows else None

    def upsert_position(self, kind, code, name, qty, cost_cents, last_cents, available):
        self.store.write(
            "INSERT INTO positions(kind, code, name, qty, available, cost_cents, last_cents, updated) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(kind, code) DO UPDATE SET "
            "name=excluded.name, qty=excluded.qty, available=excluded.available, "
            "cost_cents=excluded.cost_cents, last_cents=excluded.last_cents, updated=excluded.updated",
            (kind, str(code), str(name or ""), float(qty), float(available),
             int(cost_cents), int(last_cents), time.time()),
        )

    def delete_position(self, kind, code):
        self.store.write("DELETE FROM positions WHERE kind=? AND code=?", (kind, str(code)))

    # ---------- 成交 ----------
    def add_trade(self, idem, side, kind, code, name, qty, price_cents, amount_cents,
                  fee_cents=0, pnl_cents=0, note=""):
        return self.store.write(
            "INSERT OR IGNORE INTO trades(idem, ts, side, kind, code, name, qty, price_cents, "
            "amount_cents, fee_cents, pnl_cents, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(idem), time.time(), str(side), str(kind), str(code), str(name or ""),
             float(qty), int(price_cents), int(amount_cents), int(fee_cents), int(pnl_cents), str(note or "")),
        )

    def trades(self, limit=20):
        return self._rows("SELECT * FROM trades ORDER BY ts DESC, id DESC LIMIT ?", (max(1, int(limit)),))

    # ---------- 贷款 / 资产 ----------
    def add_loan(self, idem, kind, principal_cents, balance_cents, rate, months, monthly_cents):
        return self.store.write(
            "INSERT OR IGNORE INTO loans(idem, kind, principal_cents, balance_cents, rate, months, monthly_cents, ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (str(idem), str(kind), int(principal_cents), int(balance_cents), float(rate),
             int(months), int(monthly_cents), time.time()),
        )

    def loans(self):
        return self._rows("SELECT * FROM loans ORDER BY ts DESC")

    def add_asset(self, kind, label, value_cents):
        return self.store.write(
            "INSERT INTO assets(kind, label, value_cents, ts) VALUES(?,?,?,?)",
            (str(kind), str(label or ""), int(value_cents), time.time()),
        )

    def assets(self):
        return self._rows("SELECT * FROM assets ORDER BY ts DESC")

    # ---------- 行情缓存 ----------
    def cache_quote(self, day, code, price_cents, prev_cents, name=""):
        self.store.write(
            "INSERT INTO market_cache(day, code, price_cents, prev_cents, name, ts) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(day, code) DO UPDATE SET price_cents=excluded.price_cents, "
            "prev_cents=excluded.prev_cents, name=excluded.name, ts=excluded.ts",
            (str(day), str(code), int(price_cents), int(prev_cents), str(name or ""), time.time()),
        )

    def quote(self, day, code):
        rows = self._rows("SELECT * FROM market_cache WHERE day=? AND code=?", (str(day), str(code)))
        return rows[0] if rows else None

    # ---------- 元数据 ----------
    def meta_get(self, key, default=None):
        rows = self._rows("SELECT v FROM meta WHERE k=?", (str(key),))
        return rows[0]["v"] if rows else default

    def meta_set(self, key, value):
        return self.store.write(
            "INSERT INTO meta(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (str(key), str(value)),
        )

    # ---------- 幂等 ----------
    def settled(self, period, kind):
        return bool(self._rows("SELECT 1 FROM settlements WHERE day=? AND kind=?", (str(period), str(kind))))

    def mark_settled(self, period, kind, payload="{}"):
        return self.store.write(
            "INSERT OR IGNORE INTO settlements(day, kind, payload, created_at) VALUES(?,?,?,?)",
            (str(period), str(kind), str(payload), time.time()),
        )
