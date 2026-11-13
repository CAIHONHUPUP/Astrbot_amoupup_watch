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

    # ---------- 待入账（T+1 可取 / 基金 T+7） ----------
    def add_pending(self, idem, kind, amount_cents, due_day):
        return self.store.write(
            "INSERT OR IGNORE INTO pending_credits(idem, kind, amount_cents, due_day, created) "
            "VALUES(?,?,?,?,?)",
            (str(idem), str(kind), int(amount_cents), str(due_day), time.time()),
        )

    def due_pending(self, today):
        return self._rows(
            "SELECT * FROM pending_credits WHERE status='pending' AND due_day<=? ORDER BY due_day LIMIT 20",
            (str(today),),
        )

    def finish_pending(self, idem):
        return self.store.write(
            "UPDATE pending_credits SET status='done' WHERE idem=?", (str(idem),),
        )

    # ---------- 候选池 ----------
    def add_candidate(self, code, name="", source="user", days=5):
        import time as _t
        return self.store.write(
            "INSERT INTO candidates(code, name, source, expires_at, created) VALUES(?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, source=excluded.source, expires_at=excluded.expires_at",
            (str(code), str(name or ""), str(source or "user"),
             _t.time() + max(1, int(days)) * 86400, _t.time()),
        )

    def candidates(self):
        rows = self._rows("SELECT code, name, source, expires_at FROM candidates WHERE expires_at>? ORDER BY created DESC",
                          (time.time(),))
        return rows

    # ---------- 持仓 ----------
    def upsert_position(self, kind, code, name, qty, cost_cents, last_cents, available, ready_day=""):
        self.store.write(
            "INSERT INTO positions(kind, code, name, qty, available, cost_cents, last_cents, updated, ready_day) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(kind, code) DO UPDATE SET "
            "name=excluded.name, qty=excluded.qty, available=excluded.available, "
            "cost_cents=excluded.cost_cents, last_cents=excluded.last_cents, updated=excluded.updated, "
            "ready_day=excluded.ready_day",
            (kind, str(code), str(name or ""), float(qty), float(available),
             int(cost_cents), int(last_cents), time.time(), str(ready_day or "")),
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
        changed = self.store.write(
            "INSERT OR IGNORE INTO loans(idem, kind, principal_cents, balance_cents, rate, months, monthly_cents, ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (str(idem), str(kind), int(principal_cents), int(balance_cents), float(rate),
             int(months), int(monthly_cents), time.time()),
        )
        if not changed:
            return 0
        rows = self.store.query("SELECT id FROM loans WHERE idem=?", (str(idem),))
        return int(rows[0][0]) if rows else 0

    def add_installment(self, loan_id, seq, due_day, principal_cents, interest_cents):
        return self.store.write(
            "INSERT OR IGNORE INTO loan_installments(loan_id, seq, due_day, principal_cents, interest_cents) "
            "VALUES(?,?,?,?,?)",
            (int(loan_id), int(seq), str(due_day), int(principal_cents), int(interest_cents)),
        )

    def due_installment(self, today):
        rows = self._rows(
            "SELECT i.*, l.kind FROM loan_installments i JOIN loans l ON l.id=i.loan_id "
            "WHERE i.status='pending' AND i.due_day<=? AND l.balance_cents>0 ORDER BY i.due_day, i.seq LIMIT 1",
            (str(today),),
        )
        return rows[0] if rows else None

    def pay_installment(self, installment_id, paid_at):
        return self.store.write(
            "UPDATE loan_installments SET status='paid', paid_at=? WHERE id=?",
            (float(paid_at), int(installment_id)),
        )

    def mark_installment_overdue(self, installment_id):
        return self.store.write(
            "UPDATE loan_installments SET status='overdue' WHERE id=?", (int(installment_id),),
        )

    def update_loan(self, loan_id, balance_cents, status=""):
        if status:
            return self.store.write(
                "UPDATE loans SET balance_cents=?, status=? WHERE id=?",
                (int(balance_cents), str(status), int(loan_id)),
            )
        return self.store.write("UPDATE loans SET balance_cents=? WHERE id=?", (int(balance_cents), int(loan_id)))

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
