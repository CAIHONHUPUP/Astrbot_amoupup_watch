# -*- coding: utf-8 -*-
import asyncio
import datetime as dt

import pytest

import finance_trading as ft
from finance_store import FinanceRepo
from finance_trading import Trading
from store import Store


class FakeWallet:
    def __init__(self, cash=2000000):
        self.accounts = {"cash": cash, "withdrawable": cash}

    def balance(self, account="cash"):
        return self.accounts.get(account, 0)

    def debit(self, cents, reason, idem, ref=""):
        if self.accounts["cash"] < cents:
            return False
        self.accounts["cash"] -= cents
        if self.accounts["withdrawable"] > self.accounts["cash"]:
            self.accounts["withdrawable"] = self.accounts["cash"]
        return True

    def credit(self, cents, reason, idem, ref="", account="cash"):
        self.accounts[account] = self.accounts.get(account, 0) + cents
        return True


class FakeAmo:
    def __init__(self):
        self.wallet = FakeWallet()


class FakeFinance:
    def __init__(self):
        self.amo = FakeAmo()
        self.calls = []

    def _amo(self):
        return self.amo

    async def record_trade(self, *a, **k):
        return {"ok": True}

    async def trade_pnl(self, *a, **k):
        self.calls.append(("pnl", a))
        return {"ok": True}


class FakeRuntime:
    async def run_io(self, fn, *args, timeout=None, **kwargs):
        return fn(*args, **kwargs)


@pytest.fixture()
def env(tmp_path):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    repo = FinanceRepo(store)
    finance = FakeFinance()
    trading = Trading(None, FakeRuntime(), repo, finance)
    yield trading, repo, finance, store
    store.close()


def test_sell_cash_now_withdrawable_t_plus_1(env, monkeypatch):
    trading, repo, finance, _ = env
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    asyncio.run(trading.buy("601398", 100, price=5.0, idem="b1"))
    trading.refresh_available()
    cash_before = finance.amo.wallet.balance("cash")
    asyncio.run(trading.sell("601398", 100, price=6.0, idem="s1"))
    assert finance.amo.wallet.balance("cash") > cash_before
    rows = repo._rows("SELECT * FROM pending_credits WHERE kind='withdrawable'")
    assert rows and rows[0]["due_day"] > dt.date.today().isoformat()


def test_redeem_schedules_cash_arrival(env):
    trading, repo, finance, _ = env
    asyncio.run(trading.subscribe("110020", 100000, nav=1.0, idem="f1"))
    pos = repo.position("fund", "110020")
    trading.repo.upsert_position("fund", "110020", "", pos["qty"], pos["cost_cents"], 100, pos["qty"])
    cash_before = finance.amo.wallet.balance("cash")
    asyncio.run(trading.redeem("110020", 100, nav=1.2, idem="r1"))
    assert finance.amo.wallet.balance("cash") == cash_before  # 还没到账
    rows = repo.due_pending((dt.date.today() + dt.timedelta(days=30)).isoformat())
    assert any(r["kind"] == "cash_withdrawable" for r in rows)
