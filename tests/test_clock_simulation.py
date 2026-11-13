# -*- coding: utf-8 -*-
"""模拟时钟：把 工资→买入→T+1→涨停不可成交→亏损→5%二选一→逐笔消费→重启不丢 跑一遍。"""
import asyncio
import datetime as dt

import pytest

import finance_trading as ft
from finance_consume import FinanceConsume
from finance_help import FinanceHelp
from finance_life import FinanceLife
from finance_store import FinanceRepo
from finance_trading import Trading
from store import Store


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Wallet:
    def __init__(self, cash=0):
        self.acc = {"cash": cash, "withdrawable": cash}

    def balance(self, account="cash"):
        return self.acc.get(account, 0)

    def debit(self, cents, reason, idem, ref=""):
        if self.acc["cash"] < cents:
            return False
        self.acc["cash"] -= cents
        self.acc["withdrawable"] = min(self.acc["withdrawable"], self.acc["cash"])
        return True

    def credit(self, cents, reason, idem, ref="", account="cash"):
        self.acc[account] = self.acc.get(account, 0) + cents
        return True


class Amo:
    def __init__(self, cash=0):
        self.wallet = Wallet(cash)


class Finance:
    def __init__(self, cash=0):
        self.amo = Amo(cash)
        self.calls = []

    def _amo(self):
        return self.amo

    def balance_cents(self):
        return self.amo.wallet.balance("cash")

    def credit_initial(self, cents):
        ok = self.amo.wallet.credit(cents, "初始存款", "seed:initial")
        if ok:
            self.amo.wallet.credit(cents, "初始存款-可取", "seed:initial:w", account="withdrawable")
        return ok

    async def income(self, cents, reason, idem, salary=False):
        self.amo.wallet.credit(cents, reason, idem)
        self.calls.append(("income", cents, reason))
        return {"ok": True, "mood": "开心"}

    async def expense(self, cents, reason, idem, necessary=False):
        if not self.amo.wallet.debit(cents, reason, idem):
            return {"ok": False, "reason": "余额不足"}
        self.calls.append(("expense", cents, reason))
        return {"ok": True, "mood": "平静"}

    async def borrow(self, cents, reason, idem, months=12):
        self.amo.wallet.credit(cents, reason, idem)
        return {"ok": True}

    async def record_trade(self, *a, **k):
        return {"ok": True}

    async def trade_pnl(self, *a, **k):
        return {"ok": True}

    async def remember_event(self, *a, **k):
        return True


class Runtime:
    async def run_io(self, fn, *args, timeout=None, **kwargs):
        return fn(*args, **kwargs)


class Plugin:
    def __init__(self, repo):
        self.finance_repo = repo
        self.config = {"initial_deposit_cents": 2300000}


def make(tmp_path, now=None):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    repo = FinanceRepo(store)
    finance = Finance()
    plugin = Plugin(repo)
    trading = Trading(plugin, Runtime(), repo, finance)
    return store, repo, finance, trading, FinanceLife(plugin, repo, finance)


def test_clock_full_flow(tmp_path, monkeypatch):
    clock = Clock(dt.datetime(2026, 11, 13, 9, 35))
    monkeypatch.setattr(Trading, "_now", lambda self: clock.now)
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    monkeypatch.setattr(ft, "fetch_quotes", lambda codes: {})
    store, repo, finance, trading, life = make(tmp_path)
    plugin = Plugin(repo)

    # 1) 初始存款 + 工资（15 日）
    life.ensure_seed()
    assert finance.balance_cents() == 2300000
    clock.now = dt.datetime(2026, 11, 15, 9, 0)
    asyncio.run(life.tick(clock.now))
    assert finance.balance_cents() > 2300000
    salary = [c for c in finance.calls if c[0] == "income"]
    assert salary and salary[0][2] == "工资"
    asyncio.run(life.tick(clock.now))  # 幂等
    assert len([c for c in finance.calls if c[0] == "income"]) == 1

    # 2) 买入（加权成本）与 T+1
    clock.now = dt.datetime(2026, 11, 16, 9, 35)
    monkeypatch.setattr(trading, "_quote", lambda code, price=None: {
        "price": 5.0, "prev_close": 5.0, "open": 5.0, "high": 5.0, "low": 5.0, "name": "工商银行", "volume": 1000})
    res = asyncio.run(trading.buy("601398", 100, price=5.0, idem="b1"))
    assert res["ok"] is True
    pos = repo.position("stock", "601398")
    assert pos["qty"] == 100 and pos["available"] == 0
    assert asyncio.run(trading.sell("601398", 100, price=5.2))["ok"] is False

    # 3) 第二天 T+1 可卖
    clock.now = dt.datetime(2026, 11, 17, 9, 35)
    trading.refresh_available()
    monkeypatch.setattr(trading, "_quote", lambda code, price=None: {
        "price": 5.2, "prev_close": 5.0, "open": 5.0, "high": 5.2, "low": 5.0, "name": "工商银行", "volume": 1000})
    sell = asyncio.run(trading.sell("601398", 100, price=5.2, idem="s1"))
    assert sell["ok"] is True and "pnl" in sell

    # 4) 一字涨停买不进
    monkeypatch.setattr(trading, "_quote", lambda code, price=None: {
        "price": 5.5, "prev_close": 5.0, "open": 5.5, "high": 5.5, "low": 5.5, "name": "工商银行", "volume": 0})
    assert asyncio.run(trading.buy("601398", 100))["ok"] is False

    # 5) 行情失败不成交
    monkeypatch.setattr(trading, "_quote", lambda code, price=None: None)
    assert asyncio.run(trading.buy("601398", 100))["ok"] is False

    # 6) 5% 二选一：借款后转账被拒
    help_ = FinanceHelp(plugin, repo, finance)
    repo.meta_set("lowfund_choice", "")
    loan = asyncio.run(help_.take_loan(60000, 6, 0.06))
    assert loan["ok"] is True
    assert asyncio.run(help_.user_transfer(10000))["ok"] is False

    # 7) 逐笔消费
    consume = FinanceConsume(plugin, repo, finance)
    before = finance.balance_cents()
    asyncio.run(consume.buy_item("豆浆", 1))
    asyncio.run(consume.buy_item("肠粉", 1))
    assert finance.balance_cents() == before - 150 - 600

    # 8) 重启不丢：同一个库重新打开
    store.close()
    store2 = Store(root=tmp_path / "d", min_free_mb=0)
    store2.init()
    repo2 = FinanceRepo(store2)
    assert repo2.position("stock", "601398") is None  # 已卖出
    assert repo2.loans() and len(repo2.loans()) == 1
    assert repo2._rows("SELECT * FROM loan_installments")
    store2.close()
