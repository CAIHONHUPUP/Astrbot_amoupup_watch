# -*- coding: utf-8 -*-
"""基金交易：名称/净值落库、无估值不成交、赎回走 T+7。"""
import asyncio

import pytest

from models.db import Store
from models.ledger import FinanceRepo
import services.trade_engine as ft
from services.trade_engine import Trading


class FakeWallet:
    def __init__(self, cents=2000000):
        self.cents = cents
        self.calls = []

    def balance(self):
        return self.cents

    def debit(self, cents, reason, idem, ref=""):
        if self.cents < cents:
            return False
        self.cents -= cents
        self.calls.append(("debit", cents, reason))
        return True

    def credit(self, cents, reason, idem, ref=""):
        self.cents += cents
        self.calls.append(("credit", cents, reason))
        return True


class FakeAmo:
    def __init__(self):
        self.wallet = FakeWallet()


class FakeFinance:
    def __init__(self):
        self.amo = FakeAmo()
        self.trades = []
        self.pnls = []
        self.fund_facts = []

    def _amo(self):
        return self.amo

    async def record_trade(self, *args, **kwargs):
        self.trades.append((args, kwargs))
        return {"ok": True}

    async def record_fund_fact(self, action, name, code, amount_cents, shares=0.0, nav=0.0,
                               fee_cents=0, pnl_cents=0, idem="", when=""):
        self.fund_facts.append({
            "action": action, "name": name, "code": code, "amount_cents": amount_cents,
            "shares": shares, "nav": nav, "fee_cents": fee_cents,
            "pnl_cents": pnl_cents, "idem": idem, "when": when})
        return {"ok": True}

    async def trade_pnl(self, *args, **kwargs):
        self.pnls.append((args, kwargs))
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


def test_subscribe_with_name_uses_given_nav(env):
    trading, repo, finance, _ = env
    out = asyncio.run(trading.subscribe("005827", 50000, nav=2.0, idem="f1", name="易方达蓝筹精选混合"))
    assert out["ok"] is True
    pos = repo.position("fund", "005827")
    assert pos["name"] == "易方达蓝筹精选混合"
    assert finance.amo.wallet.calls[-1] == ("debit", 50000, "buy_fund")
    assert finance.fund_facts[-1]["action"] == "buy_fund"
    assert finance.fund_facts[-1]["name"] == "易方达蓝筹精选混合"
    assert finance.trades == []


def test_subscribe_without_nav_rejects_when_not_tradeable(env, monkeypatch):
    trading, repo, finance, _ = env
    monkeypatch.setattr(ft, "fetch_fund_quote", lambda code: {
        "code": code, "name": "兜底", "nav": 2.0, "estimate": 2.0,
        "change_pct": 0.0, "source": "pingzhongdata", "tradeable": False})
    before = finance.amo.wallet.cents
    out = asyncio.run(trading.subscribe("005827", 50000, nav=0, idem="f2"))
    assert out["ok"] is False and "估值" in out["message"]
    assert finance.amo.wallet.cents == before


def test_subscribe_without_nav_uses_estimate(env, monkeypatch):
    trading, repo, finance, _ = env
    monkeypatch.setattr(ft, "fetch_fund_quote", lambda code: {
        "code": code, "name": "易方达蓝筹精选混合", "nav": 2.0, "estimate": 2.5,
        "change_pct": 8.0, "source": "fundgz", "tradeable": True})
    out = asyncio.run(trading.subscribe("005827", 100000, nav=0, idem="f3"))
    assert out["ok"] is True
    # 1000 元，费 1.2 元，998.8 / 2.5 = 399.52 份
    assert abs(out["shares"] - 399.52) < 0.01
    assert repo.position("fund", "005827")["name"] == "易方达蓝筹精选混合"


def test_redeem_rejects_when_no_estimate(env, monkeypatch):
    trading, repo, _, _ = env
    repo.upsert_position("fund", "005827", "易方达", 100.0, 20000, 20000, 100.0)
    monkeypatch.setattr(ft, "fetch_fund_quote", lambda code: None)
    out = asyncio.run(trading.redeem("005827", 50, nav=0, idem="r1"))
    assert out["ok"] is False and "估值" in out["message"]
    assert repo.position("fund", "005827")["qty"] == 100.0


def test_redeem_with_nav_writes_pending_and_pnl(env):
    trading, repo, finance, _ = env
    repo.upsert_position("fund", "005827", "易方达", 100.0, 20000, 20000, 100.0)
    out = asyncio.run(trading.redeem("005827", 50, nav=2.5, idem="r2"))
    assert out["ok"] is True
    assert repo.position("fund", "005827")["qty"] == 50.0
    pending = repo._rows("SELECT * FROM pending_credits WHERE idem=?", ("r2",))
    assert pending and pending[0]["kind"] == "cash_withdrawable"
    assert finance.pnls
    assert finance.fund_facts[-1]["action"] == "sell_fund"
    assert finance.fund_facts[-1]["shares"] == 50.0
