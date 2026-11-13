# -*- coding: utf-8 -*-
import asyncio

import pytest

import finance_trading as ft
from finance_market import parse_quotes
from finance_store import FinanceRepo
from finance_trading import Trading
from store import Store


class FakeWallet:
    def __init__(self, cents=2000000):
        self.cents = cents
        self.seen = []

    def balance(self):
        return self.cents

    def debit(self, cents, reason, idem, ref=""):
        if self.cents < cents:
            return False
        self.cents -= cents
        self.seen.append(("debit", cents, idem))
        return True

    def credit(self, cents, reason, idem, ref=""):
        self.cents += cents
        self.seen.append(("credit", cents, idem))
        return True


class FakeAmo:
    def __init__(self, cents=2000000):
        self.wallet = FakeWallet(cents)


class FakeFinance:
    def __init__(self, cents=2000000):
        self.amo = FakeAmo(cents)
        self.calls = []

    def _amo(self):
        return self.amo

    async def record_trade(self, *args, **kwargs):
        self.calls.append(("record_trade", args, kwargs))
        return {"ok": True}

    async def trade_pnl(self, *args, **kwargs):
        self.calls.append(("trade_pnl", args, kwargs))
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


def test_buy_costs_fees_and_updates_position(env, monkeypatch):
    trading, repo, finance, _ = env
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    res = asyncio.run(trading.buy("600519", 100, price=100.0, idem="b1"))
    assert res["ok"] is True, res
    # 100 元 * 100 股 = 1,000,000 分；佣金 500 + 过户费 10
    pos = repo.position("stock", "600519")
    assert pos["qty"] == 100 and pos["available"] == 0
    assert finance.amo.wallet.cents == 2000000 - 1000000 - 510
    assert finance.calls and finance.calls[0][0] == "record_trade"


def test_buy_rejects_non_lot(env, monkeypatch):
    trading, _, _, _ = env
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    assert asyncio.run(trading.buy("600519", 150, price=100.0))["ok"] is False


def test_buy_rejects_outside_session(env):
    trading, _, _, _ = env
    res = asyncio.run(trading.buy("600519", 100, price=100.0))
    assert res["ok"] is False and "交易时段" in res["message"]


def test_buy_rejects_price_limit(env, monkeypatch):
    trading, _, _, _ = env
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    monkeypatch.setattr(trading, "_quote", lambda code, price=None: {"price": 120.0, "prev_close": 100.0, "name": "x"})
    res = asyncio.run(trading.buy("600519", 100, price=120.0))
    assert res["ok"] is False and "涨跌幅" in res["message"]


def test_buy_rejects_insufficient_balance(env, monkeypatch):
    trading, _, finance, _ = env
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    finance.amo.wallet.cents = 100
    res = asyncio.run(trading.buy("600519", 100, price=100.0))
    assert res["ok"] is False and "余额不足" in res["message"]


def test_sell_needs_t_plus_1(env, monkeypatch):
    trading, repo, finance, _ = env
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    asyncio.run(trading.buy("600519", 100, price=100.0, idem="b2"))
    res = asyncio.run(trading.sell("600519", 100, price=105.0))
    assert res["ok"] is False and "T+1" in res["message"]
    trading.refresh_available()
    pos = repo.position("stock", "600519")
    assert pos["available"] == 100
    res = asyncio.run(trading.sell("600519", 100, price=105.0, idem="s2"))
    assert res["ok"] is True, res
    assert repo.position("stock", "600519") is None
    assert finance.calls[-1][0] == "trade_pnl"


def test_fund_subscribe_fee_and_shares(env):
    trading, repo, _, _ = env
    res = asyncio.run(trading.subscribe("005827", 100000, nav=2.0, idem="f1"))
    assert res["ok"] is True
    # 1000 元，申购费 0.12% -> 1.2 元；998.8 / 2.0 = 499.4 份
    assert abs(res["shares"] - 499.4) < 0.01
    assert abs(repo.position("fund", "005827")["qty"] - 499.4) < 0.01


def test_quote_parser():
    text = 'v_sh600519="1~贵州茅台~600519~1680.00~1670.00~1685.00~";'
    rows = parse_quotes(text)
    assert rows["600519"]["name"] == "贵州茅台"
    assert rows["600519"]["price"] == 1680.0
    assert rows["600519"]["prev_close"] == 1670.0
